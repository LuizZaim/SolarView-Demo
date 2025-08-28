# dashboard_server.py
"""
Backend Flask com a lógica de KPI aprimorada para o Pico Solar.
"""
from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash
from datetime import datetime, time as dtime
import pandas as pd
import os
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

load_dotenv()

from sems_connector import SemsConnector

# --- Configuração ---
app = Flask(__name__, template_folder='templates')
app.secret_key = os.urandom(24)

VALID_USERNAME = os.getenv("SEMS_ACCOUNT", "demo@goodwe.com")
VALID_PASSWORD_HASH = os.getenv("SEMS_PASSWORD_HASH")
INVERTER_ID = os.getenv("INVERTER_ID", "5010KETU229W6177")

client = SemsConnector(
    account=VALID_USERNAME,
    password=os.getenv("SEMS_PASSWORD", "GoodweSems123!@#"),
    login_region="us",
    data_region="eu"
)

DATA_CACHE = {}
executor = ThreadPoolExecutor(max_workers=4)


# --- Funções Auxiliares ---

def parse_sems_timeseries(response_json: dict, column_name: str) -> pd.DataFrame:
    if not isinstance(response_json, dict): return pd.DataFrame()
    items = []
    data_obj = response_json.get('data')
    if isinstance(data_obj, dict):
        for key in ('column1', 'items', 'list', 'datas', 'result'):
            if key in data_obj and isinstance(data_obj[key], list):
                items = data_obj[key];
                break
    if not items:
        for key in ('data', 'items', 'list', 'result', 'datas'):
            if key in response_json and isinstance(response_json[key], list):
                items = response_json[key];
                break
    if not items: return pd.DataFrame()
    records = []
    for item in items:
        if not isinstance(item, dict): continue
        timestamp = item.get('time') or item.get('date') or item.get('collectTime') or item.get('cTime') or item.get(
            'tm')
        value = item.get(column_name) or item.get('value') or item.get('v') or item.get('val') or item.get('column')
        if timestamp and value is not None:
            try:
                ts_parsed = pd.to_datetime(timestamp, errors='coerce')
                if pd.isna(ts_parsed): ts_parsed = pd.to_datetime(timestamp, dayfirst=True, errors='coerce')
                if not pd.isna(ts_parsed):
                    records.append({'time': ts_parsed, column_name: float(str(value).replace(',', '.'))})
            except (ValueError, TypeError):
                continue
    if not records: return pd.DataFrame()
    return pd.DataFrame(records).sort_values(by='time').reset_index(drop=True)


def calculate_kpis(df: pd.DataFrame) -> dict:
    """Calcula as métricas de resumo (KPIs) com a lógica aprimorada para o Pico Solar."""
    if df.empty: return {}

    total_energy = df['Eday'].dropna().iloc[-1] if 'Eday' in df and not df['Eday'].dropna().empty else 0.0
    soc_series = df['Cbattery1'].dropna()
    soc_initial = soc_series.iloc[0] if not soc_series.empty else None
    soc_final = soc_series.iloc[-1] if not soc_series.empty else None

    # Lógica aprimorada para o Pico de Potência
    peak_power = 0.0
    # Só calculamos o pico se a energia total for maior que zero.
    if total_energy > 0 and 'Pac' in df.columns and not df['Pac'].dropna().empty:
        try:
            pac_series = pd.to_numeric(df['Pac'], errors='coerce').dropna()
            if not pac_series.empty:
                peak_power = pac_series.max()
        except Exception:
            peak_power = 0.0

    return {
        "total_energy": round(total_energy, 2),
        "peak_power": round(peak_power, 2),
        "soc_initial": int(soc_initial) if soc_initial is not None else None,
        "soc_final": int(soc_final) if soc_final is not None else None,
    }


def gerar_analise_simples(kpis: dict, lang: str = 'pt') -> str:
    translations = {'pt': {'no_data': "Não foi possível gerar a análise pois não há dados suficientes.",
                           'exc': "O dia foi excecionalmente produtivo, com uma geração total de {total_energy} kWh.",
                           'good': "O dia teve uma boa produção de energia, atingindo {total_energy} kWh.",
                           'modest': "A produção de energia foi modesta, com um total de {total_energy} kWh.",
                           'low': "A produção de energia foi baixa, totalizando apenas {total_energy} kWh.",
                           'charge': "A bateria terminou o dia com mais carga do que começou, indicando um bom excedente energético.",
                           'discharge': "Foi necessário utilizar uma parte da energia armazenada na bateria para suprir o consumo.",
                           'stable': "O estado da bateria manteve-se estável ao longo do dia."},
                    'en': {'no_data': "Could not generate analysis due to insufficient data.",
                           'exc': "The day was exceptionally productive, with a total generation of {total_energy} kWh.",
                           'good': "The day had good energy production, reaching {total_energy} kWh.",
                           'modest': "Energy production was modest, with a total of {total_energy} kWh.",
                           'low': "Energy production was low, totaling only {total_energy} kWh.",
                           'charge': "The battery ended the day with more charge than it started, indicating a good energy surplus.",
                           'discharge': "It was necessary to use a portion of the stored battery energy to meet consumption.",
                           'stable': "The battery's state of charge remained stable throughout the day."}}
    t = translations.get(lang, translations['pt'])
    if not kpis: return t['no_data']
    total_energy = kpis.get("total_energy", 0)
    soc_initial = kpis.get("soc_initial")
    soc_final = kpis.get("soc_final")
    if total_energy > 30:
        analise_energia = t['exc'].format(total_energy=total_energy)
    elif total_energy > 15:
        analise_energia = t['good'].format(total_energy=total_energy)
    elif total_energy > 5:
        analise_energia = t['modest'].format(total_energy=total_energy)
    else:
        analise_energia = t['low'].format(total_energy=total_energy)
    analise_bateria = ""
    if soc_initial is not None and soc_final is not None:
        if soc_final > soc_initial:
            analise_bateria = t['charge']
        elif soc_final < soc_initial:
            analise_bateria = t['discharge']
        else:
            analise_bateria = t['stable']
    return f"{analise_energia} {analise_bateria}".strip()


# --- Rotas de Autenticação e Navegação ---

@app.route('/')
def home():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == VALID_USERNAME and VALID_PASSWORD_HASH and check_password_hash(VALID_PASSWORD_HASH, password):
            session['logged_in'] = True
            session['settings'] = {'theme': 'system', 'language': 'pt'}
            flash('Login bem-sucedido!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Credenciais inválidas. Por favor, tente novamente.', 'danger')
    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session:
        flash('Por favor, faça login para aceder ao dashboard.', 'info')
        return redirect(url_for('login'))
    return render_template('dashboard.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão terminada com sucesso.', 'info')
    return redirect(url_for('login'))


@app.route('/settings')
def settings():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    return render_template('settings.html')


# --- Rotas de API ---

@app.route('/api/data')
def get_dashboard_data():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    if date_str in DATA_CACHE: return jsonify(DATA_CACHE[date_str])
    try:
        columns_to_fetch = ['Pac', 'Cbattery1', 'pgrid', 'Eday']
        query_datetime = datetime.combine(datetime.fromisoformat(date_str), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in
                 columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty: all_dfs.append(df)
        if not all_dfs: return jsonify({"error": f"Nenhum dado encontrado para a data {date_str}."}), 404
        merged_df = all_dfs[0]
        for df_next in all_dfs[1:]:
            merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time',
                                      direction='nearest', tolerance=pd.Timedelta('5min'))
        merged_df = merged_df.ffill().bfill()
        kpis = calculate_kpis(merged_df)
        chart_data = {'timestamps': merged_df['time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(), 'series': {}}
        for col_name in ['Pac', 'Cbattery1', 'pgrid']:
            if col_name in merged_df.columns: chart_data['series'][col_name] = merged_df[col_name].tolist()
        final_result = {"kpis": kpis, "charts": chart_data}
        DATA_CACHE[date_str] = final_result
        return jsonify(final_result)
    except Exception as e:
        print(f"Ocorreu um erro inesperado no servidor: {e}");
        import traceback;
        traceback.print_exc()
        return jsonify({"error": "Ocorreu um erro interno no servidor."}), 500


@app.route('/api/analyze', methods=['POST'])
def analyze_data():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    if not request.is_json: return jsonify({"error": "O corpo do pedido deve ser JSON"}), 400
    data = request.get_json()
    kpis = data.get('kpis')
    lang = session.get('settings', {}).get('language', 'pt')
    if not kpis or not isinstance(kpis, dict): return jsonify(
        {"error": "KPIs não fornecidos ou em formato inválido"}), 400
    try:
        analysis_text = gerar_analise_simples(kpis, lang)
        return jsonify({"analysis": analysis_text})
    except Exception as e:
        print(f"Erro ao gerar análise simples: {e}")
        return jsonify({"error": "Não foi possível gerar a análise."}), 500


@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    settings = session.get('settings', {'theme': 'system', 'language': 'pt'})
    return jsonify(settings)


@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    if not request.is_json: return jsonify({"error": "O corpo do pedido deve ser JSON"}), 400
    new_settings = request.get_json()
    theme = new_settings.get('theme', 'system')
    language = new_settings.get('language', 'pt')
    session['settings'] = {'theme': theme, 'language': language}
    session.modified = True
    return jsonify({"message": "Configurações guardadas com sucesso!"})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
