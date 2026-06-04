import os
import json
import uuid
from io import BytesIO

import pandas as pd
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_file, flash, jsonify
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_store')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


def _get_dataset_path(dataset_id):
    return os.path.join(DATA_DIR, f'{dataset_id}.csv')


def _load_dataframe(dataset_id):
    path = _get_dataset_path(dataset_id)
    if not os.path.exists(path):
        return None
    return pd.read_csv(path)


def _save_dataframe(df, dataset_id=None):
    if dataset_id is None:
        dataset_id = uuid.uuid4().hex
    path = _get_dataset_path(dataset_id)
    df.to_csv(path, index=False)
    return dataset_id


def _push_history(dataset_id):
    """保存当前数据快照到新 ID，加入历史栈"""
    src = _get_dataset_path(dataset_id)
    if not os.path.exists(src):
        return
    import shutil
    snapshot_id = uuid.uuid4().hex
    shutil.copy2(src, _get_dataset_path(snapshot_id))
    history = session.get('_undo_history', [])
    history.append(snapshot_id)
    if len(history) > 50:
        # 清理旧快照文件
        old = history.pop(0)
        old_path = _get_dataset_path(old)
        if os.path.exists(old_path):
            os.remove(old_path)
    session['_undo_history'] = history


def _pop_history():
    """从历史栈弹出最近快照 ID"""
    history = session.get('_undo_history', [])
    if not history:
        return None
    return history.pop()


@app.route('/')
def index():
    dataset_id = session.get('dataset_id')
    df = None
    columns = []
    rows = []
    filename = session.get('filename', '')

    if dataset_id:
        df = _load_dataframe(dataset_id)
        if df is not None:
            columns = df.columns.tolist()
            try:
                rows = df.head(200).fillna('').to_dict(orient='records')
            except Exception:
                rows = df.head(200).astype(str).to_dict(orient='records')

    return render_template('index.html', columns=columns, rows=rows,
                           filename=filename, row_count=len(df) if df is not None else 0,
                           col_count=len(columns))


def _read_csv(file):
    encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1', 'iso-8859-1']
    for enc in encodings:
        try:
            file.seek(0)
            return pd.read_csv(file, encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    file.seek(0)
    return pd.read_csv(file, encoding='utf-8', errors='replace')


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file or file.filename == '':
        flash('请选择一个文件', 'error')
        return redirect(url_for('index'))

    ext = os.path.splitext(file.filename)[1].lower()
    try:
        if ext == '.csv':
            df = _read_csv(file)
        elif ext in ('.xlsx', '.xls'):
            df = pd.read_excel(file)
        else:
            flash(f'不支持的文件格式: {ext}，请上传 CSV 或 Excel 文件', 'error')
            return redirect(url_for('index'))
    except Exception as e:
        flash(f'文件读取失败: {str(e)}', 'error')
        return redirect(url_for('index'))

    if df.empty:
        flash('文件为空或无法解析', 'error')
        return redirect(url_for('index'))

    dataset_id = _save_dataframe(df)
    session['dataset_id'] = dataset_id
    session['filename'] = file.filename
    session['_undo_history'] = []
    flash(f'成功加载 {file.filename}，共 {len(df)} 行 {len(df.columns)} 列', 'success')
    return redirect(url_for('index'))


@app.route('/process-column', methods=['POST'])
def process_column():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        flash('请先上传数据文件', 'error')
        return redirect(url_for('index'))

    df = _load_dataframe(dataset_id)
    if df is None:
        flash('数据集已过期，请重新上传', 'error')
        return redirect(url_for('index'))

    column = request.form.get('column')
    operation = request.form.get('operation')
    param = request.form.get('param', '').strip()

    if column not in df.columns:
        flash('无效的列名', 'error')
        return redirect(url_for('index'))

    _push_history(dataset_id)
    col_data = df[column]
    try:
        if operation == 'fillna':
            df[column] = col_data.fillna(param if param else '')

        elif operation == 'dropna':
            df = df[col_data.notna()].reset_index(drop=True)

        elif operation == 'normalize':
            numeric_col = pd.to_numeric(col_data, errors='coerce')
            mn, mx = numeric_col.min(), numeric_col.max()
            if mx - mn > 0:
                df[column] = (numeric_col - mn) / (mx - mn)
            else:
                flash('该列最大值等于最小值，无法归一化', 'warning')

        elif operation == 'standardize':
            numeric_col = pd.to_numeric(col_data, errors='coerce')
            mu, sigma = numeric_col.mean(), numeric_col.std()
            if sigma and sigma > 0:
                df[column] = (numeric_col - mu) / sigma
            else:
                flash('该列标准差为0，无法标准化', 'warning')

        elif operation == 'strip':
            df[column] = col_data.astype(str).str.strip()

        elif operation == 'duplicate':
            new_name = param if param else f'{column}_copy'
            df[new_name] = col_data

        elif operation == 'sort_asc':
            df = df.sort_values(by=column, ascending=True).reset_index(drop=True)

        elif operation == 'sort_desc':
            df = df.sort_values(by=column, ascending=False).reset_index(drop=True)

        elif operation == 'delete':
            if len(df.columns) <= 1:
                flash('至少保留一列数据', 'error')
                return redirect(url_for('index'))
            df = df.drop(columns=[column])

        else:
            flash(f'未知操作: {operation}', 'error')
            return redirect(url_for('index'))

    except Exception as e:
        flash(f'处理失败: {str(e)}', 'error')
        return redirect(url_for('index'))

    session['dataset_id'] = _save_dataframe(df, dataset_id)
    flash(f'列 "{column}" 的 {operation} 操作完成', 'success')
    return redirect(url_for('index'))


@app.route('/edit-cell', methods=['POST'])
def edit_cell():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        return jsonify({'ok': False, 'error': '请先上传数据文件'})

    df = _load_dataframe(dataset_id)
    if df is None:
        return jsonify({'ok': False, 'error': '数据集已过期'})

    data = request.get_json()
    row_idx = data.get('row')
    column = data.get('column')
    new_value = data.get('value', '')

    if column not in df.columns:
        return jsonify({'ok': False, 'error': '无效的列名'})

    if row_idx < 0 or row_idx >= len(df):
        return jsonify({'ok': False, 'error': '无效的行索引'})

    _push_history(dataset_id)
    df.at[row_idx, column] = new_value
    session['dataset_id'] = _save_dataframe(df, dataset_id)
    return jsonify({'ok': True, 'value': new_value})


@app.route('/export')
def export():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        flash('请先上传数据文件', 'error')
        return redirect(url_for('index'))

    df = _load_dataframe(dataset_id)
    if df is None:
        flash('数据集已过期，请重新上传', 'error')
        return redirect(url_for('index'))

    fmt = request.args.get('format', 'csv')
    buf = BytesIO()
    filename = session.get('filename', 'data')

    if fmt == 'xlsx':
        base = os.path.splitext(filename)[0]
        output_name = f'{base}_processed.xlsx'
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Sheet1')
        buf.seek(0)
        return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         as_attachment=True, download_name=output_name)
    else:
        base = os.path.splitext(filename)[0]
        output_name = f'{base}_processed.csv'
        df.to_csv(buf, index=False)
        buf.seek(0)
        return send_file(buf, mimetype='text/csv',
                         as_attachment=True, download_name=output_name)


@app.route('/undo', methods=['POST'])
def undo():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        return jsonify({'ok': False, 'error': '无数据'})

    prev_id = _pop_history()
    if prev_id is None:
        return jsonify({'ok': False, 'error': '没有可回退的上一步'})

    if not os.path.exists(_get_dataset_path(prev_id)):
        return jsonify({'ok': False, 'error': '历史数据已过期'})

    session['dataset_id'] = prev_id
    session['_undo_history'] = session.get('_undo_history', [])
    return jsonify({'ok': True})


@app.route('/undo-count')
def undo_count():
    history = session.get('_undo_history', [])
    return jsonify({'count': len(history)})


@app.route('/reset')
def reset():
    session.pop('dataset_id', None)
    session.pop('filename', None)
    session.pop('_undo_history', None)
    flash('数据已清除', 'info')
    return redirect(url_for('index'))


@app.route('/stats')
def stats():
    dataset_id = session.get('dataset_id')
    if not dataset_id:
        return jsonify({'ok': False, 'error': '无数据'})

    df = _load_dataframe(dataset_id)
    if df is None:
        return jsonify({'ok': False, 'error': '数据集已过期'})

    desc = df.describe(include='all').fillna('').to_dict()
    dtypes = {col: str(dt) for col, dt in df.dtypes.items()}
    missing = df.isnull().sum().to_dict()
    return jsonify({'ok': True, 'describe': desc, 'dtypes': dtypes, 'missing': missing,
                    'shape': list(df.shape)})


if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
