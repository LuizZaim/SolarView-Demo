import os
import random
from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash, send_from_directory
from datetime import datetime, time as dtime, timedelta
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()
from sems_connector import SemsConnector

# --- Configuração ---
app = Flask(__name__, template_folder='templates')
app.secret_key = os.urandom(24)

# Credenciais padrão (sem depender de .env)
VALID_USERNAME = "demo@goodwe.com"
# Senha padrão segura (hash da senha "GoodweSems123!@#")
VALID_PASSWORD_HASH = "scrypt:32768:8:1$17X25Qps6qaJDA6q$bb9f80a06295191d792794e3f5d68ec6d6199e1625eba58a540c1a831d7000e675a100b8e9093ff68bf2b2a335f329a7f66d96a83ca78e43693c37096ed78149"

# ID do inversor
INVERTER_ID = "5010KETU229W6177"

client = SemsConnector(
    account=VALID_USERNAME,
    password="GoodweSems123!@#",
    login_region="us",
    data_region="eu"
)

DATA_CACHE = {}
executor = ThreadPoolExecutor(max_workers=4)

# 🔑 Chave da API OpenWeatherMap
OPENWEATHER_API_KEY = "c032e66b7ccaf5e84bb2e4014e85ea38"
# 📍 Coordenadas do sistema solar (ex: São Paulo)
LATITUDE = -23.5505
LONGITUDE = -46.6333

# --- NOVO: Variável de estado para a bomba de água e o reservatório ---
WATER_STATUS = {
    "level": 75,  # Nível de água em porcentagem
    "pump_on": False,
    "mode": "auto"  # 'auto', 'manual', 'emergency'
}

# --- NOVO: Estado dos dispositivos inteligentes ---
DEVICES_STATUS = {
    "ac": False,      # Ar-condicionado desligado
    "tv": False       # Televisão desligada
}

# --- Funções Auxiliares ---
def parse_sems_timeseries(response_json: dict, column_name: str) -> pd.DataFrame:
    """Função para analisar dados da API Goodwe e transformar em DataFrame."""
    if not isinstance(response_json, dict): return pd.DataFrame()
    items = []
    data_obj = response_json.get('data')
    if isinstance(data_obj, dict):
        for key in ('column1', 'items', 'list', 'datas', 'result'):
            if key in data_obj and isinstance(data_obj[key], list):
                items = data_obj[key]
                break
    if not items:
        for key in ('data', 'items', 'list', 'result', 'datas'):
            if key in response_json and isinstance(response_json[key], list):
                items = response_json[key]
                break
    if not items: return pd.DataFrame()
    records = []
    for item in items:
        if not isinstance(item, dict): continue
        timestamp = item.get('time') or item.get('date') or item.get('collectTime') or item.get('cTime') or item.get('tm')
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
    """Calcula as métricas de resumo (KPIs)."""
    if df.empty:
        return {}
    total_energy = df['Eday'].dropna().iloc[-1] if 'Eday' in df and not df['Eday'].dropna().empty else 0.0
    peak_power = df['Pac'].dropna().max() if 'Pac' in df and not df['Pac'].dropna().empty else 0.0
    soc_initial = None
    soc_final = None
    if 'Cbattery1' in df.columns:
        soc_series = df['Cbattery1'].dropna()
        soc_initial = soc_series.iloc[0] if not soc_series.empty else None
        soc_final = soc_series.iloc[-1] if not soc_series.empty else None
    return {
        "total_energy": round(total_energy, 2),
        "peak_power": round(peak_power, 2),
        "soc_initial": int(soc_initial) if soc_initial is not None else None,
        "soc_final": int(soc_final) if soc_final is not None else None,
    }

def get_weather_forecast_real(date: datetime) -> dict:
    """
    Busca previsão do tempo real usando OpenWeatherMap (One Call API).
    Retorna dados detalhados para o dia.
    """
    try:
        url = f"https://api.openweathermap.org/data/3.0/onecall"
        params = {
            'lat': LATITUDE,
            'lon': LONGITUDE,
            'exclude': 'current,minutely,hourly,alerts',
            'units': 'metric',
            'appid': OPENWEATHER_API_KEY
        }
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            raise Exception(f"API retornou status {response.status_code}")
        data = response.json()
        # Procurar previsão para a data solicitada
        target_date = date.date()
        for day in data['daily']:
            day_date = datetime.fromtimestamp(day['dt']).date()
            if day_date == target_date:
                weather_main = day['weather'][0]['main'].lower()
                temp_max = round(day['temp']['max'], 1)
                pop = day['pop']  # Probabilidade de precipitação (0 a 1)
                uv = day['uvi']
                description = day['weather'][0]['description']
                return {
                    'condition': weather_main,
                    'temp_max': temp_max,
                    'pop': pop,
                    'uv': uv,
                    'description': description
                }
        # Se não encontrar, use o primeiro dia (hoje)
        day = data['daily'][0]
        weather_main = day['weather'][0]['main'].lower()
        temp_max = round(day['temp']['max'], 1)
        pop = day['pop']
        uv = day['uvi']
        description = day['weather'][0]['description']
        return {
            'condition': weather_main,
            'temp_max': temp_max,
            'pop': pop,
            'uv': uv,
            'description': description
        }
    except Exception as e:
        print(f"Erro ao buscar clima: {e}")
        # Fallback seguro
        return {
            'condition': 'cloudy',
            'temp_max': 25.0,
            'pop': 0.3,
            'uv': 5,
            'description': 'nublado'
        }

# ✅ FUNÇÃO ATUALIZADA: Análise com foco em consumo noturno vs. oportunidade diurna
def analise_consumo_vs_producao(df: pd.DataFrame, lang: str = 'pt') -> dict:
    translations = {
        'pt': {
            'low_autonomy': "A autonomia da sua casa em relação à energia solar foi baixa hoje. Você consumiu cerca de {pgrid_percent:.0f}% da energia diretamente da rede, principalmente no final do dia.",
            'high_autonomy': "Sua casa foi altamente autossuficiente hoje! A maior parte do consumo foi atendida pela energia solar gerada.",
            'no_grid_consumption': "Parabéns! Sua casa foi 100% autossuficiente hoje, utilizando apenas energia solar.",
            'heavy_night_usage': "Você tem o hábito de usar aparelhos pesados à noite. Em dias ensolarados, tente programá-los entre 10h e 15h para aproveitar a energia solar gratuita.",
            'daytime_opportunity': "Seu sistema gera muita energia durante o dia, mas você não está aproveitando totalmente. Considere ligar máquinas de lavar, secadoras ou aquecedores nesse período.",
            'grid_dependent_evening': "Seu consumo aumenta significativamente após o pôr do sol. Isso reduz sua economia. Use a bateria ou desloque tarefas para o dia.",
            'no_data': "Não foi possível analisar o consumo da rede por falta de dados."
        }
    }
    t = translations.get(lang)
    if df.empty or 'pgrid' not in df.columns or 'Pac' not in df.columns:
        # Fallback para demonstração
        return {
            'analysis': "Sua casa foi altamente autossuficiente hoje! 85% da energia veio dos painéis solares.",
            'recommendation': "Parabéns! Continue assim."
        }

    df = df.copy()
    df['time'] = pd.to_datetime(df['time'])
    df['hour'] = df['time'].dt.hour

    energy_generated_total = df['Pac'].sum() / (60 * 1000)
    energy_from_grid = df[df['pgrid'] > 0]['pgrid'].sum() / (60 * 1000)
    pgrid_percent = (energy_from_grid / (energy_from_grid + energy_generated_total)) * 100 if (energy_from_grid + energy_generated_total) > 0 else 0

    analysis_text = ""
    recommendation_text = ""

    if pgrid_percent > 20:
        analysis_text = t['low_autonomy'].format(pgrid_percent=pgrid_percent)
    elif pgrid_percent > 0:
        analysis_text = t['high_autonomy']
    else:
        analysis_text = t['no_grid_consumption']

    # Análise de consumo noturno (20h–6h)
    night_df = df[(df['hour'] >= 20) | (df['hour'] < 6)]
    night_grid = night_df[night_df['pgrid'] > 0]['pgrid'].sum() / (60 * 1000)

    # Análise de geração diurna (10h–15h)
    day_peak_df = df[(df['hour'] >= 10) & (df['hour'] <= 15)]
    day_solar_avg = day_peak_df['Pac'].mean() if not day_peak_df.empty else 0
    day_grid_use = day_peak_df[day_peak_df['pgrid'] > 0]['pgrid'].sum() / (60 * 1000)

    if night_grid > 2.0:
        recommendation_text = t['heavy_night_usage']
    elif day_solar_avg > 3000 and day_grid_use > 1.0:
        recommendation_text = t['daytime_opportunity']
    elif night_grid > 1.0:
        recommendation_text = t['grid_dependent_evening']

    return {'analysis': analysis_text, 'recommendation': recommendation_text}

# ✅ FUNÇÃO ATUALIZADA: Recomendações com foco em aparelhos pesados
def gerar_recomendacoes(kpis: dict, merged_df: pd.DataFrame, lang: str = 'pt') -> list:
    """Gera recomendações específicas baseadas nos dados"""
    translations = {
        'pt': {
            'recommend_cleaning': "Considere limpar os painéis solares para melhorar a eficiência.",
            'recommend_monitoring': "Monitore o consumo noturno para preservar a bateria.",
            'recommend_increase_usage': "Excelente produção! Considere aumentar o consumo durante o dia.",
            'recommend_energy_shift': "Tente deslocar o consumo para o período de maior geração solar.",
            'recommend_program_heavy_devices': "Programe aparelhos pesados (máquina de lavar, forno, ar-condicionado) para funcionarem em dias ensolarados entre 10h e 15h."
        }
    }
    t = translations.get(lang, translations['pt'])
    recomendacoes = []
    total_energy = kpis.get("total_energy", 0)
    soc_final = kpis.get("soc_final")
    peak_power = kpis.get("peak_power", 0)

    if total_energy < 10:
        recomendacoes.append(t['recommend_cleaning'])
    if soc_final and soc_final < 20:
        recomendacoes.append(t['recommend_monitoring'])
    if total_energy > 30:
        recomendacoes.append(t['recommend_increase_usage'])
    if peak_power < 2000:
        recomendacoes.append(t['recommend_energy_shift'])

    # ✅ Sempre incluir recomendação comportamental se houver boa produção
    if total_energy > 15:
        recomendacoes.append(t['recommend_program_heavy_devices'])

    return recomendacoes

def gerar_analise_melhorada(kpis: dict, history_data: dict, merged_df: pd.DataFrame, date_str: str, lang: str = 'pt') -> str:
    # Gera uma análise textual mais detalhada com base nos KPIs do dia
    translations = {
        'pt': {
            'no_data': "Não foi possível gerar a análise pois não há dados suficientes.",
            'high_efficiency': "Excelente eficiência! A produção superou as expectativas.",
            'good_efficiency': "Boa eficiência energética observada.",
            'moderate_efficiency': "Eficiência moderada.",
            'low_efficiency': "Eficiência abaixo do esperado.",
            'battery_charge': "A bateria carregou significativamente {soc_delta}% (de {soc_initial}% para {soc_final}%).",
            'battery_discharge': "A bateria descarregou {soc_delta}% (de {soc_initial}% para {soc_final}%).",
            'battery_stable': "O estado da bateria permaneceu estável (de {soc_initial}% para {soc_final}%).",
            'no_battery': "Não foram registados dados da bateria para este dia.",
            'great_peak': "Pico de geração muito bom.",
            'good_peak': "Pico de geração adequado.",
            'low_peak': "Pico de geração pode ser melhorado.",
            'summary_excellent': "Em resumo, foi um dia de excelente autossuficiência energética.",
            'summary_good': "Em resumo, foi um dia de bom desempenho e autossuficiência.",
            'summary_ok': "Em resumo, foi um dia de desempenho modesto.",
            'summary_bad': "Em resumo, foi um dia de baixa performance.",
            'comparison_above_avg': "A produção de energia de hoje, de {total_energy} kWh, está {delta_energy_percent}% acima da média histórica.",
            'comparison_below_avg': "A produção de energia de hoje, de {total_energy} kWh, está {delta_energy_percent}% abaixo da média histórica.",
            'comparison_stable': "A produção de energia de hoje, de {total_energy} kWh, está alinhada com a média histórica.",
        }
    }
    t = translations.get(lang, translations['pt'])
    if not kpis: return t['no_data']
    total_energy = kpis.get("total_energy", 0)
    peak_power = kpis.get("peak_power", 0)
    soc_initial = kpis.get("soc_initial")
    soc_final = kpis.get("soc_final")

    analise_comparativa = ""
    temp_history = {date: data for date, data in history_data.items() if date != date_str and data.get('total_energy') is not None}
    if temp_history:
        total_energy_list = [d['total_energy'] for d in temp_history.values()]
        if total_energy_list:
            avg_energy = sum(total_energy_list) / len(total_energy_list)
            delta_energy = total_energy - avg_energy
            if avg_energy != 0:
                delta_energy_percent = abs(round(delta_energy / avg_energy * 100, 1))
            else:
                delta_energy_percent = 0
            if delta_energy > 1:
                analise_comparativa = t['comparison_above_avg'].format(total_energy=total_energy, delta_energy_percent=delta_energy_percent)
            elif delta_energy < -1:
                analise_comparativa = t['comparison_below_avg'].format(total_energy=total_energy, delta_energy_percent=delta_energy_percent)
            else:
                analise_comparativa = t['comparison_stable'].format(total_energy=total_energy)

    expected_energy = 20
    efficiency = (total_energy / expected_energy * 100) if expected_energy > 0 else 0
    if efficiency > 120:
        analise_energia = t['high_efficiency']
    elif efficiency > 80:
        analise_energia = t['good_efficiency']
    elif efficiency > 50:
        analise_energia = t['moderate_efficiency']
    else:
        analise_energia = t['low_efficiency']

    analise_bateria = ""
    if soc_initial is not None and soc_final is not None:
        soc_delta = abs(soc_final - soc_initial)
        if soc_final > soc_initial + 5:
            analise_bateria = t['battery_charge'].format(soc_delta=soc_delta, soc_initial=soc_initial, soc_final=soc_final)
        elif soc_final < soc_initial - 5:
            analise_bateria = t['battery_discharge'].format(soc_delta=soc_delta, soc_initial=soc_initial, soc_final=soc_final)
        else:
            analise_bateria = t['battery_stable'].format(soc_initial=soc_initial, soc_final=soc_final)
    else:
        analise_bateria = t['no_battery']

    analise_pico = t['great_peak'] if peak_power > 5000 else t['good_peak'] if peak_power > 3000 else t['low_peak']
    analise_consumo_dict = analise_consumo_vs_producao(merged_df, lang)
    analise_consumo_texto = analise_consumo_dict['analysis']
    recomendacao_consumo = analise_consumo_dict['recommendation']

    # 🔔 Previsão do tempo real
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    clima_hoje = get_weather_forecast_real(date_obj)
    clima_amanha = get_weather_forecast_real(date_obj + timedelta(days=1))
    analise_preditiva = ""
    if clima_amanha['condition'] in ['rain', 'drizzle']:
        analise_preditiva = f"A previsão indica {clima_amanha['description']} amanhã (probabilidade de chuva: {int(clima_amanha['pop'] * 100)}%). Considere carregar sua bateria hoje para se preparar."
    elif clima_amanha['condition'] == 'clouds':
        analise_preditiva = f"Amanhã estará nublado, o que pode reduzir sua eficiência solar. Monitore o consumo à tarde."
    elif clima_amanha['condition'] == 'clear':
        analise_preditiva = f"Excelente notícia! Amanhã será um dia ensolarado com temperatura máxima de {clima_amanha['temp_max']}°C. Programe o uso de aparelhos pesados entre 10h e 15h para aproveitar ao máximo a energia solar gratuita!"
    else:
        analise_preditiva = f"Amanhã: {clima_amanha['description']}. Aproveite para otimizar o uso da energia solar."

    if total_energy > 25:
        conclusao = t['summary_excellent']
    elif total_energy > 15:
        conclusao = t['summary_good']
    elif total_energy > 8:
        conclusao = t['summary_ok']
    else:
        conclusao = t['summary_bad']

    return f"{analise_comparativa} {analise_energia} {analise_bateria} {analise_pico} {analise_consumo_texto} {conclusao} {analise_preditiva} {recomendacao_consumo}".strip()

def gerar_sugestoes_automacao(kpis: dict, merged_df: pd.DataFrame, lang: str = 'pt') -> list:
    """
    Gera sugestões de automação com base nos dados do momento.
    """
    translations = {
        'pt': {
            'battery_full_suggestion': "Sua bateria está quase cheia, utilize-a! Que tal ligar o ar-condicionado ou a máquina de lavar?",
            'peak_generation_suggestion': "A produção solar está no seu pico! Aproveite para ligar o aquecedor de água e economizar.",
            'low_production_warning': "A produção solar está baixa. Para evitar usar energia da rede, que tal desligar a televisão e as luzes desnecessárias?"
        }
    }
    t = translations.get(lang, translations['pt'])
    sugestoes = []
    current_time = datetime.now()
    current_hour = current_time.hour
    soc_final = kpis.get('soc_final', 0)
    peak_power = kpis.get('peak_power', 0)
    if soc_final > 90 and 10 <= current_hour <= 16:
        sugestoes.append(t['battery_full_suggestion'])
    if peak_power > 4000 and 11 <= current_hour <= 15:
        sugestoes.append(t['peak_generation_suggestion'])
    if peak_power < 1000 and 18 <= current_hour <= 21:
        sugestoes.append(t['low_production_warning'])
    return sugestoes

def add_to_history(date):
    """Função auxiliar para adicionar data ao histórico"""
    if 'logged_in' in session:
        try:
            date_obj = datetime.fromisoformat(date)
            today = datetime.now().date()
            if date_obj.date() > today:
                return  # Não adicionar datas futuras
        except ValueError:
            return  # Data inválida
        history = session.get('history', [])
        if date in history:
            history.remove(date)
        if len(history) >= 50:
            history = history[-49:]
        history.append(date)
        session['history'] = history
        session.modified = True

def get_alexa_energy_data(intent_name: str, column_name: str, unit: str, date_str: str = None):
    """Função auxiliar para buscar dados da API e formatar a resposta da Alexa."""
    date_to_query = date_str if date_str else datetime.now().strftime('%Y-%m-%d')
    try:
        response_json = client.get_inverter_data_by_column(INVERTER_ID, column_name, date_to_query)
        df = parse_sems_timeseries(response_json, column_name)
        if not df.empty and column_name in df.columns:
            value = df[column_name].dropna().iloc[-1] if column_name == 'Eday' else df[column_name].dropna().max()
            try:
                date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
                today = datetime.now().date()
                yesterday = today - timedelta(days=1)
                if date_obj.date() == today:
                    date_text = "hoje"
                elif date_obj.date() == yesterday:
                    date_text = "ontem"
                else:
                    meses_pt = {'January': 'janeiro', 'February': 'fevereiro', 'March': 'março', 'April': 'abril',
                                'May': 'maio', 'June': 'junho', 'July': 'julho', 'August': 'agosto',
                                'September': 'setembro', 'October': 'outubro', 'November': 'novembro',
                                'December': 'dezembro'}
                    month_name_en = date_obj.strftime('%B')
                    month_name_pt = meses_pt.get(month_name_en, month_name_en)
                    date_text = f"{date_obj.day} de {month_name_pt}"
            except Exception as e:
                print(f"Erro ao formatar data para fala: {e}")
                date_text = "uma data desconhecida"
            speech_text = f"A energia total gerada em {value:.2f} {unit}."
        else:
            # ✅ Fallback: dados mockados
            print(f"⚠️ Dados reais não encontrados para {date_to_query} — usando dados simulados.")
            times = pd.date_range(start=f"{date_to_query} 06:00", end=f"{date_to_query} 18:00", freq='15min')
            df = pd.DataFrame({
                'time': times,
                'Eday': [round(random.uniform(8.0, 25.0), 2) for _ in times],
                'Pac': [random.randint(1000, 4000) for _ in times]
            })
            df['Eday'] = df['Eday'].cummax()
            value = df[column_name].iloc[-1] if column_name == 'Eday' else df[column_name].max()
            try:
                date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
                today = datetime.now().date()
                yesterday = today - timedelta(days=1)
                if date_obj.date() == today:
                    date_text = "hoje"
                elif date_obj.date() == yesterday:
                    date_text = "ontem"
                else:
                    meses_pt = {'January': 'janeiro', 'February': 'fevereiro', 'March': 'março', 'April': 'abril',
                                'May': 'maio', 'June': 'junho', 'July': 'julho', 'August': 'agosto',
                                'September': 'setembro', 'October': 'outubro', 'November': 'novembro',
                                'December': 'dezembro'}
                    month_name_en = date_obj.strftime('%B')
                    month_name_pt = meses_pt.get(month_name_en, month_name_en)
                    date_text = f"{date_obj.day} de {month_name_pt}"
            except:
                date_text = "uma data desconhecida"
            speech_text = f"A energia total gerada em {date_text} foi de {value:.2f} {unit}."
    except Exception as e:
        print(f"Erro ao buscar dados da API para Alexa: {e}")
        speech_text = "Desculpe, não consegui obter os dados no momento."
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech_text},
            "shouldEndSession": True
        }
    }

def get_alexa_analysis_data(date_str: str = None):
    """Gera uma análise inteligente do dia para a Alexa."""
    date_to_query = date_str if date_str else datetime.now().strftime('%Y-%m-%d')
    try:
        columns_to_fetch = ['Pac', 'Cbattery1', 'Eday', 'pgrid']
        query_datetime = datetime.combine(datetime.fromisoformat(date_to_query), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty:
                all_dfs.append(df)
        if not all_dfs:
            raise Exception("Nenhum dado encontrado")
        merged_df = all_dfs[0]
        for df_next in all_dfs[1:]:
            merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time', direction='nearest', tolerance=pd.Timedelta('5min'))
        merged_df = merged_df.ffill().bfill()
        kpis = calculate_kpis(merged_df)
        if not kpis:
            raise Exception("KPIs não calculados")
        history_data = session.get('history_data', {})
        analysis_text = gerar_analise_melhorada(kpis, history_data, merged_df, date_to_query, 'pt')
        recomendacoes = gerar_recomendacoes(kpis, merged_df, 'pt')
        sugestoes_automacao = gerar_sugestoes_automacao(kpis, merged_df, 'pt')
        # 🔔 Clima para Alexa
        date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
        clima_amanha = get_weather_forecast_real(date_obj + timedelta(days=1))
        try:
            date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            if date_obj.date() == today:
                date_text = "hoje"
            elif date_obj.date() == yesterday:
                date_text = "ontem"
            else:
                meses_pt = {'January': 'janeiro', 'February': 'fevereiro', 'March': 'março', 'April': 'abril',
                            'May': 'maio', 'June': 'junho', 'July': 'julho', 'August': 'agosto',
                            'September': 'setembro', 'October': 'outubro', 'November': 'novembro',
                            'December': 'dezembro'}
                month_name_en = date_obj.strftime('%B')
                month_name_pt = meses_pt.get(month_name_en, month_name_en)
                date_text = f"{date_obj.day} de {month_name_pt}"
        except Exception as e:
            print(f"Erro ao formatar data para fala: {e}")
            date_text = "uma data desconhecida"
        speech_text = f"Análise de {date_text}: {analysis_text}. "
        if recomendacoes:
            speech_text += f"Aqui estão algumas recomendações: " + " ".join([f"{rec}. <break time='500ms'/>" for rec in recomendacoes])
        if sugestoes_automacao:
            speech_text += f"Nossa IA tem algumas sugestões de automação para você: " + " ".join([f"{sug}. <break time='500ms'/>" for sug in sugestoes_automacao])
        # 🔔 Adiciona previsão do tempo
        if clima_amanha['condition'] == 'rain':
            speech_text += f" Para amanhã, a previsão é de chuva com {int(clima_amanha['pop'] * 100)}% de chance. Carregue sua bateria hoje para se preparar."
        elif clima_amanha['condition'] == 'clear':
            speech_text += f" Boas notícias! Amanhã será um dia ensolarado. Aproveite para usar aparelhos pesados entre 10h e 15h."
    except Exception as e:
        print(f"Erro ao gerar análise para Alexa: {e}")
        speech_text = "Desculpe, não consegui gerar a análise no momento."
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "SSML",
                "ssml": f"<speak>{speech_text}</speak>"
            },
            "shouldEndSession": True
        }
    }

# --- NOVAS FUNÇÕES PARA AS NOVAS INTENTS ---
def get_alexa_autonomy_report(date_str: str = None):
    """Retorna um relatório de autossuficiência e consumo da rede."""
    date_to_query = date_str if date_str else datetime.now().strftime('%Y-%m-%d')
    try:
        columns_to_fetch = ['Pac', 'pgrid', 'Eday']
        query_datetime = datetime.combine(datetime.fromisoformat(date_to_query), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty:
                all_dfs.append(df)
        if not all_dfs:
            raise Exception("Nenhum dado encontrado")
        merged_df = all_dfs[0]
        for df_next in all_dfs[1:]:
            merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time', direction='nearest', tolerance=pd.Timedelta('5min'))
        merged_df = merged_df.ffill().bfill()
        kpis = calculate_kpis(merged_df)
        if not kpis:
            raise Exception("KPIs não calculados")
        analise_consumo_dict = analise_consumo_vs_producao(merged_df, 'pt')
        analysis_text = analise_consumo_dict['analysis']
        recommendation_text = analise_consumo_dict['recommendation']
        try:
            date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            if date_obj.date() == today:
                date_text = "hoje"
            elif date_obj.date() == yesterday:
                date_text = "ontem"
            else:
                meses_pt = {'January': 'janeiro', 'February': 'fevereiro', 'March': 'março', 'April': 'abril',
                            'May': 'maio', 'June': 'junho', 'July': 'julho', 'August': 'agosto',
                            'September': 'setembro', 'October': 'outubro', 'November': 'novembro',
                            'December': 'dezembro'}
                month_name_en = date_obj.strftime('%B')
                month_name_pt = meses_pt.get(month_name_en, month_name_en)
                date_text = f"{date_obj.day} de {month_name_pt}"
        except Exception as e:
            print(f"Erro ao formatar data para fala: {e}")
            date_text = "uma data desconhecida"
        speech_text = f"Relatório de autossuficiência de {date_text}: {analysis_text}"
        if recommendation_text:
            speech_text += f" {recommendation_text}"
    except Exception as e:
        print(f"Erro ao gerar relatório de autossuficiência para Alexa: {e}")
        speech_text = "Desculpe, não consegui gerar o relatório de autossuficiência no momento."
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech_text},
            "shouldEndSession": True
        }
    }

def get_alexa_automation_suggestion():
    """Retorna uma sugestão de automação com base nos dados atuais."""
    try:
        date_to_query = datetime.now().strftime('%Y-%m-%d')
        columns_to_fetch = ['Pac', 'Cbattery1', 'Eday', 'pgrid']
        query_datetime = datetime.combine(datetime.fromisoformat(date_to_query), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty:
                all_dfs.append(df)
        if not all_dfs:
            raise Exception("Nenhum dado encontrado")
        merged_df = all_dfs[0]
        for df_next in all_dfs[1:]:
            merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time', direction='nearest', tolerance=pd.Timedelta('5min'))
        merged_df = merged_df.ffill().bfill()
        kpis = calculate_kpis(merged_df)
        if not kpis:
            raise Exception("KPIs não calculados")
        sugestoes_automacao = gerar_sugestoes_automacao(kpis, merged_df, 'pt')
        speech_text = f"Aqui está uma sugestão de automação para você: {sugestoes_automacao[0]}" if sugestoes_automacao else "No momento, não tenho sugestões de automação específicas. Mas continue monitorando sua geração solar!"
    except Exception as e:
        print(f"Erro ao gerar sugestão de automação para Alexa: {e}")
        speech_text = "Desculpe, não consegui gerar uma sugestão de automação no momento."
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech_text},
            "shouldEndSession": True
        }
    }

# --- NOVA: Função para obter a previsão do tempo para a Alexa ---
def get_alexa_weather_data(date_str: str = None):
    """Retorna a previsão do tempo para a Alexa."""
    date_to_query = date_str if date_str else datetime.now().strftime('%Y-%m-%d')
    try:
        date_obj = datetime.strptime(date_to_query, '%Y-%m-%d')
        weather_data = get_weather_forecast_real(date_obj)
        condition = weather_data.get('description', 'dados indisponíveis')
        temp_max = weather_data.get('temp_max', 'dados indisponíveis')
        pop = int(weather_data.get('pop', 0) * 100)
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        if date_obj.date() == today:
            date_text = "para hoje"
        elif date_obj.date() == yesterday:
            date_text = "para ontem"
        else:
            meses_pt = {'January': 'janeiro', 'February': 'fevereiro', 'March': 'março', 'April': 'abril',
                        'May': 'maio', 'June': 'junho', 'July': 'julho', 'August': 'agosto', 'September': 'setembro',
                        'October': 'outubro', 'November': 'novembro', 'December': 'dezembro'}
            month_name_en = date_obj.strftime('%B')
            month_name_pt = meses_pt.get(month_name_en, month_name_en)
            date_text = f"para {date_obj.day} de {month_name_pt}"
        speech_text = f"A previsão do tempo {date_text} é de {condition}, com temperatura máxima de {temp_max}°C. Há {pop}% de chance de chuva."
    except Exception as e:
        print(f"Erro ao gerar previsão do tempo para Alexa: {e}")
        speech_text = "Desculpe, não consegui obter a previsão do tempo no momento."
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": speech_text},
            "shouldEndSession": True
        }
    }

# --- Rotas de Autenticação e Navegação ---
@app.route('/')
def home():
    if 'logged_in' in session: return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == VALID_USERNAME and VALID_PASSWORD_HASH and check_password_hash(
                VALID_PASSWORD_HASH, request.form['password']):
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
    # Inicializa 'settings' se não existir na sessão
    if 'settings' not in session:
        session['settings'] = {'theme': 'system', 'language': 'pt'}
    return render_template('dashboard.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão terminada com sucesso.', 'info')
    return redirect(url_for('login'))

@app.route('/settings')
def settings():
    if 'logged_in' not in session:
        flash('Por favor, faça login para aceder às configurações.', 'info')
        return redirect(url_for('login'))
    # Inicializa 'settings' se não existir na sessão
    if 'settings' not in session:
        session['settings'] = {'theme': 'system', 'language': 'pt'}
    return render_template('settings.html')

# --- Rota para a página de Controle de Água ---
@app.route('/water-control')
def water_control():
    if 'logged_in' not in session:
        flash('Por favor, faça login para aceder a esta página.', 'info')
        return redirect(url_for('login'))
    return render_template('water_control.html')

# --- Rotas de API para o Controle de Água ---
@app.route('/api/water_status', methods=['GET'])
def get_water_status_api():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    # Simula consumo e recarga da água
    if WATER_STATUS['pump_on']:
        WATER_STATUS['level'] = min(100, WATER_STATUS['level'] + 2)
    else:
        WATER_STATUS['level'] = max(0, WATER_STATUS['level'] - 1)
    # Lógica de automação
    current_hour = datetime.now().hour
    if WATER_STATUS['mode'] == 'auto':
        if 10 <= current_hour <= 16 and WATER_STATUS['level'] < 90:
            WATER_STATUS['pump_on'] = True
        elif WATER_STATUS['level'] > 95 or not (10 <= current_hour <= 16):
            WATER_STATUS['pump_on'] = False
    # Lógica de emergência
    if WATER_STATUS['mode'] == 'emergency' and WATER_STATUS['level'] < 100:
        WATER_STATUS['pump_on'] = True
    elif WATER_STATUS['mode'] == 'emergency' and WATER_STATUS['level'] >= 100:
        WATER_STATUS['pump_on'] = False
        WATER_STATUS['mode'] = 'manual' # Volta para o modo manual após encher
    return jsonify(WATER_STATUS)

@app.route('/api/toggle_pump', methods=['POST'])
def toggle_pump():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    # Altera o modo para manual para permitir controle
    WATER_STATUS['mode'] = 'manual'
    WATER_STATUS['pump_on'] = not WATER_STATUS['pump_on']
    return jsonify(WATER_STATUS)

@app.route('/api/set_water_mode', methods=['POST'])
def set_water_mode():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    data = request.get_json()
    mode = data.get('mode')
    if mode in ['auto', 'manual', 'emergency']:
        WATER_STATUS['mode'] = mode
        if mode == 'manual':
             WATER_STATUS['pump_on'] = False
        elif mode == 'auto':
             WATER_STATUS['pump_on'] = False
        elif mode == 'emergency':
            WATER_STATUS['pump_on'] = True # Liga a bomba para encher
    return jsonify(WATER_STATUS)

# --- 🔌 NOVAS ROTAS: Automação de Dispositivos ---
@app.route('/api/device_status', methods=['GET'])
def get_device_status():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    return jsonify(DEVICES_STATUS)

@app.route('/api/toggle_device', methods=['POST'])
def toggle_device():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    data = request.get_json()
    device = data.get('device')
    if device not in DEVICES_STATUS:
        return jsonify({"error": "Dispositivo inválido"}), 400
    DEVICES_STATUS[device] = not DEVICES_STATUS[device]
    return jsonify({
        "success": True,
        "device": device,
        "state": DEVICES_STATUS[device]
    })

# --- Rotas de API do Dashboard ---
@app.route('/api/data')
def get_dashboard_data():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    try:
        date_obj = datetime.fromisoformat(date_str)
        today = datetime.now().date()
        if date_obj.date() > today:
            return jsonify({"error": "Dados não disponíveis para datas futuras."}), 400
    except ValueError:
        return jsonify({"error": "Formato de data inválido"}), 400
    add_to_history(date_str)
    if date_str in DATA_CACHE: return jsonify(DATA_CACHE[date_str])
    try:
        columns_to_fetch = ['Pac', 'Cbattery1', 'pgrid', 'Eday']
        query_datetime = datetime.combine(datetime.fromisoformat(date_str), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty: all_dfs.append(df)
        if not all_dfs:
            # ✅ Fallback: dados mockados
            print(f"⚠️ Dados reais não encontrados para {date_str} — usando dados simulados.")
            times = pd.date_range(start=f"{date_str} 06:00", end=f"{date_str} 18:00", freq='15min')
            df_mock = pd.DataFrame({
                'time': times,
                'Pac': [random.randint(1000, 4000) for _ in times],
                'Cbattery1': [random.randint(40, 90) for _ in times],
                'pgrid': [random.randint(0, 500) for _ in times],
                'Eday': [round(random.uniform(8.0, 25.0), 2) for _ in times]
            })
            df_mock['Eday'] = df_mock['Eday'].cummax()
            kpis = calculate_kpis(df_mock)
            chart_data = {
                'timestamps': df_mock['time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
                'series': {
                    'Pac': df_mock['Pac'].tolist(),
                    'Cbattery1': df_mock['Cbattery1'].tolist(),
                    'pgrid': df_mock['pgrid'].tolist()
                }
            }
            final_result = {"kpis": kpis, "charts": chart_data}
            DATA_CACHE[date_str] = final_result
            return jsonify(final_result)
        merged_df = all_dfs[0]
        for df_next in all_dfs[1:]:
            merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time', direction='nearest', tolerance=pd.Timedelta('5min'))
        merged_df = merged_df.ffill().bfill()
        kpis = calculate_kpis(merged_df)
        chart_data = {'timestamps': merged_df['time'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(), 'series': {}}
        for col_name in ['Pac', 'Cbattery1', 'pgrid']:
            if col_name in merged_df.columns: chart_data['series'][col_name] = merged_df[col_name].tolist()
        final_result = {"kpis": kpis, "charts": chart_data}
        DATA_CACHE[date_str] = final_result
        history_data = session.get('history_data', {})
        history_data[date_str] = {'total_energy': kpis['total_energy'], 'peak_power': kpis['peak_power']}
        session['history_data'] = history_data
        session.modified = True
        return jsonify(final_result)
    except Exception as e:
        print(f"Ocorreu um erro inesperado no servidor: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Ocorreu um erro interno no servidor."}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze_data():
    if 'logged_in' not in session: return jsonify({"error": "Acesso não autorizado"}), 401
    if not request.is_json: return jsonify({"error": "O corpo do pedido deve ser JSON"}), 400
    data = request.get_json()
    kpis = data.get('kpis')
    date_str = data.get('date')
    lang = session.get('settings', {}).get('language', 'pt')
    if not kpis or not isinstance(kpis, dict): return jsonify({"error": "KPIs não fornecidos ou em formato inválido"}), 400
    try:
        history_data = session.get('history_data', {})
        avg_energy = 0
        temp_history = {date: data for date, data in history_data.items() if date != date_str and data.get('total_energy') is not None}
        if temp_history:
            total_energy_list = [d['total_energy'] for d in temp_history.values()]
            if total_energy_list:
                avg_energy = round(sum(total_energy_list) / len(total_energy_list), 2)
        columns_to_fetch = ['Pac', 'Cbattery1', 'pgrid', 'Eday']
        query_datetime = datetime.combine(datetime.fromisoformat(date_str), dtime(0, 0)).strftime("%Y-%m-%d %H:%M:%S")
        tasks = [lambda col=col: client.get_inverter_data_by_column(INVERTER_ID, col, query_datetime) for col in columns_to_fetch]
        results = list(executor.map(lambda f: f(), tasks))
        all_dfs = []
        for i, col_name in enumerate(columns_to_fetch):
            df = parse_sems_timeseries(results[i], col_name)
            if not df.empty:
                all_dfs.append(df)
        if not all_dfs:
            merged_df = pd.DataFrame()
        else:
            merged_df = all_dfs[0]
            for df_next in all_dfs[1:]:
                merged_df = pd.merge_asof(merged_df.sort_values('time'), df_next.sort_values('time'), on='time', direction='nearest', tolerance=pd.Timedelta('5min'))
            merged_df = merged_df.ffill().bfill()
        analysis_text = gerar_analise_melhorada(kpis, history_data, merged_df, date_str, lang)
        recomendacoes = gerar_recomendacoes(kpis, merged_df, lang)
        chart_data = {'labels': ['Hoje', 'Média Histórica'], 'values': [kpis['total_energy'], avg_energy]}
        full_analysis = analysis_text
        if recomendacoes:
            full_analysis += "\nRecomendações:"
            for i, rec in enumerate(recomendacoes, 1):
                full_analysis += f"\n{i}. {rec}"
        return jsonify({"analysis": full_analysis, "recomendacoes": recomendacoes, "chart_data": chart_data})
    except Exception as e:
        print(f"Erro ao gerar análise simples: {e}")
        return jsonify({"error": "Não foi possível gerar a análise."}), 500

@app.route('/api/alexa', methods=['POST'])
def handle_alexa_request():
    request_json = request.get_json()
    print(f"Alexa Request: {request_json}")
    request_type = request_json['request']['type']
    if request_type == 'LaunchRequest':
        speech_text = "Bem-vindo ao Solar View! Você pode me perguntar sobre a energia total, o pico de potência, um resumo do dia, autossuficiência ou sugestões de automação."
        return jsonify({
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": speech_text},
                "reprompt": {"outputSpeech": {"type": "PlainText", "text": "O que você gostaria de saber? Por exemplo: 'qual foi a energia gerada hoje?'"}},
                "shouldEndSession": False
            }
        })
    elif request_type == 'IntentRequest':
        intent = request_json['request']['intent']
        intent_name = intent['name']
        date_slot_value = None
        if 'slots' in intent and 'date' in intent['slots'] and 'value' in intent['slots']['date']:
            date_slot_value = intent['slots']['date']['value']
        today = datetime.now().date()
        # LÓGICA CORRIGIDA AQUI:
        if date_slot_value:
            if date_slot_value.upper() == 'TODAY':
                date_to_query = today.strftime('%Y-%m-%d')
            elif date_slot_value.upper() == 'YESTERDAY':
                date_to_query = (today - timedelta(days=1)).strftime('%Y-%m-%d')
            elif date_slot_value.upper() == 'TOMORROW':
                date_to_query = (today + timedelta(days=1)).strftime('%Y-%m-%d')
            elif len(date_slot_value) == 10 and date_slot_value[4] == '-' and date_slot_value[7] == '-':
                date_to_query = date_slot_value
            else:
                try:
                    parsed_date = pd.to_datetime(date_slot_value)
                    date_to_query = parsed_date.strftime('%Y-%m-%d')
                except Exception as e:
                    date_to_query = today.strftime('%Y-%m-%d')
                    print(f"Não foi possível interpretar a data '{date_slot_value}', usando hoje. Erro: {e}")
        else:
            # Esta é a parte corrigida para a sua pergunta:
            if intent_name == 'GetDailyEnergyIntent':
                date_to_query = (today - timedelta(days=1)).strftime('%Y-%m-%d')  # Ontem (lógica original)
            elif intent_name == 'GetWeatherForecastIntent':
                date_to_query = (today + timedelta(days=1)).strftime('%Y-%m-%d')  # Amanhã (nova lógica)
            else:
                date_to_query = today.strftime('%Y-%m-%d')  # Hoje (padrão para outros)
            print(f"Slot 'date' vazio — usando fallback: {date_to_query}")
        print(f"Date to query (normalized): {date_to_query}")
        if intent_name == 'GetDailyEnergyIntent':
            return jsonify(get_alexa_energy_data(intent_name, 'Eday', 'quilowatts-hora', date_to_query))
        elif intent_name == 'GetPeakPowerIntent':
            return jsonify(get_alexa_energy_data(intent_name, 'Pac', 'watts', date_to_query))
        elif intent_name == 'GetDailyAnalysisIntent':
            return jsonify(get_alexa_analysis_data(date_to_query))
        elif intent_name == 'GetAutonomyReportIntent':
            return jsonify(get_alexa_autonomy_report(date_to_query))
        elif intent_name == 'GetAutomationSuggestionIntent':
            return jsonify(get_alexa_automation_suggestion())
        elif intent_name == 'GetWeatherForecastIntent':
            return jsonify(get_alexa_weather_data(date_to_query))
        else:
            speech_text = "Desculpe, não entendi. Você pode perguntar sobre a energia total, o pico de potência, um resumo do dia, autossuficiência, clima ou sugestões de automação."
            return jsonify({
                "version": "1.0",
                "response": {
                    "outputSpeech": {"type": "PlainText", "text": speech_text},
                    "reprompt": {"outputSpeech": {"type": "PlainText", "text": "O que você gostaria de saber sobre sua geração solar?"}},
                    "shouldEndSession": False
                }
            })
    return jsonify({"error": "Tipo de requisição não suportado"}), 400

@app.route('/api/get_settings', methods=['GET'])
def get_settings():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    settings = session.get('settings', {'theme': 'system', 'language': 'pt'})
    return jsonify(settings)

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    if not request.is_json:
        return jsonify({"error": "O corpo do pedido deve ser JSON"}), 400
    new_settings = request.get_json()
    theme = new_settings.get('theme', 'system')
    language = new_settings.get('language', 'pt')
    session['settings'] = {'theme': theme, 'language': language}
    session.modified = True
    return jsonify({"message": "Configurações guardadas com sucesso!"})

# --- Rotas de Favoritos e Histórico ---
@app.route('/api/favorites', methods=['GET'])
def get_favorites():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    favorites = session.get('favorites', [])
    return jsonify(favorites)

@app.route('/api/favorites', methods=['POST'])
def add_favorite():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    if not request.is_json:
        return jsonify({"error": "Dados inválidos"}), 400
    data = request.get_json()
    date = data.get('date')
    if not date:
        return jsonify({"error": "Data não fornecida"}), 400
    try:
        datetime.fromisoformat(date)
    except ValueError:
        return jsonify({"error": "Formato de data inválido"}), 400
    favorites = session.get('favorites', [])
    if len(favorites) >= 20:
        favorites = favorites[-19:]
    if date not in favorites:
        favorites.append(date)
        session['favorites'] = favorites
        session.modified = True
        return jsonify({"message": "Adicionado aos favoritos", "favorites": favorites})
    else:
        return jsonify({"message": "Data já está nos favoritos", "favorites": favorites})

@app.route('/api/favorites/<date>', methods=['DELETE'])
def remove_favorite(date):
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    favorites = session.get('favorites', [])
    if date in favorites:
        favorites.remove(date)
        session['favorites'] = favorites
        session.modified = True
        return jsonify({"message": "Removido dos favoritos", "favorites": favorites})
    else:
        return jsonify({"message": "Data não encontrada nos favoritos", "favorites": favorites})

@app.route('/api/history', methods=['GET'])
def get_history():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    history = session.get('history', [])
    return jsonify(history[-15:])

# --- Rota para a página de Previsão Solar ---
@app.route('/previsao')
def previsao():
    if 'logged_in' not in session:
        flash('Por favor, faça login para acessar esta página.', 'info')
        return redirect(url_for('login'))
    # Inicializa 'settings' se não existir na sessão
    if 'settings' not in session:
        session['settings'] = {'theme': 'system', 'language': 'pt'}
    return render_template('previsao.html', settings=session['settings'])

# --- Rota da API de Previsão Solar ---
@app.route('/api/previsao')
def api_previsao():
    if 'logged_in' not in session:
        return jsonify({"error": "Acesso não autorizado"}), 401
    try:
        previsao = []
        for i in range(7):
            date_obj = datetime.now() + timedelta(days=i)
            clima = get_weather_forecast_real(date_obj)
            # Fator de eficiência baseado no clima
            if clima['condition'] == 'clear':
                fator = 1.0
            elif clima['condition'] == 'clouds':
                fator = 0.6
            elif clima['condition'] in ['rain', 'drizzle']:
                fator = 0.3
            else:
                fator = 0.7
            energia_base = 25.0  # média histórica
            energia_prevista = round(energia_base * fator, 1)
            previsao.append({
                'dia': date_obj.strftime('%a'),
                'energia': energia_prevista,
                'clima': clima['description']
            })
        return jsonify(previsao)
    except Exception as e:
        print(f"Erro ao gerar previsão: {e}")
        return jsonify({"error": "Não foi possível gerar a previsão."}), 500

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('frontend/static', filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)