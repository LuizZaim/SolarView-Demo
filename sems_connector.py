# sems_connector.py
"""
Módulo dedicado para interagir com a API do SEMS Portal (GoodWe).
"""
import json
import base64
import requests
from typing import Literal, Dict, Any

# URLs base para as diferentes regiões da API.
BASE_URLS = {
    "us": "https://us.semsportal.com",
    "eu": "https://eu.semsportal.com",
}


class SemsConnector:
    """
    Cliente para a API SEMS da GoodWe que gere a autenticação,
    o token de sessão e os pedidos de dados.
    """

    def __init__(self, account: str, password: str, login_region: Literal["us", "eu"] = "us",
                 data_region: Literal["us", "eu"] = "eu"):
        """
        Inicializa o conector com as credenciais e regiões corretas.
        Para a conta demo, o login é na região 'us' e os dados na 'eu'.
        """
        self.login_base_url = BASE_URLS.get(login_region, BASE_URLS["us"])
        self.data_base_url = BASE_URLS.get(data_region, BASE_URLS["eu"])
        self.account = account
        self.password = password
        self.token = None

    def _get_initial_token(self) -> str:
        """Gera o token inicial (pré-login) necessário para a primeira requisição."""
        initial_data = {"uid": "", "timestamp": 0, "token": "", "client": "web", "version": "", "language": "en"}
        encoded_data = json.dumps(initial_data).encode("utf-8")
        return base64.b64encode(encoded_data).decode("utf-8")

    def login(self) -> bool:
        """
        Realiza o login na API, obtém e armazena o token de sessão.
        Retorna True em caso de sucesso, False caso contrário.
        """
        print("A realizar login na API GoodWe...")
        url = f"{self.login_base_url}/api/v2/common/crosslogin"
        # Headers e Payload idênticos aos dos ficheiros de exemplo para garantir compatibilidade.
        headers = {"Token": self._get_initial_token(), "Content-Type": "application/json", "Accept": "*/*"}
        payload = {"account": self.account, "pwd": self.password}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()
            if "data" not in data or data.get("code") not in (0, 1, 200):
                print(f"Login falhou: {data}")
                return False
            data_to_encode = json.dumps(data["data"]).encode("utf-8")
            self.token = base64.b64encode(data_to_encode).decode("utf-8")
            print("Login bem-sucedido.")
            return True
        except requests.RequestException as e:
            print(f"Erro de conexão durante o login: {e}")
            self.token = None
            return False

    def get_inverter_data_by_column(self, inverter_id: str, column: str, date: str) -> Dict[str, Any]:
        """
        Busca os dados de uma coluna específica para um inversor.
        Se o token expirar, tenta fazer login novamente uma vez.
        """
        # Garante que temos um token válido antes de fazer o pedido.
        if not self.token and not self.login():
            return {}

        # Usa a URL da região de DADOS, que pode ser diferente da de login.
        url = f"{self.data_base_url}/api/PowerStationMonitor/GetInverterDataByColumn"
        headers = {"Token": self.token, "Content-Type": "application/json", "Accept": "*/*"}
        payload = {"date": date, "column": column, "id": inverter_id}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Erro ao buscar dados da coluna '{column}': {e}")
            # Se o erro for de autorização (401/403), o token pode ter expirado.
            if e.response and e.response.status_code in [401, 403]:
                print("Token pode ter expirado. A tentar fazer login novamente...")
                if self.login():
                    print("Login refeito com sucesso. A tentar buscar os dados novamente...")
                    headers["Token"] = self.token  # Atualiza o header com o novo token
                    try:
                        retry_response = requests.post(url, json=payload, headers=headers, timeout=20)
                        retry_response.raise_for_status()
                        return retry_response.json()
                    except requests.RequestException as retry_e:
                        print(f"A segunda tentativa de buscar dados também falhou: {retry_e}")
            return {}
