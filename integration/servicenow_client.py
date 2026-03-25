import requests
from config.settings import settings


# ---------------- TOKEN ----------------
def get_oauth_token():

    url = f"{settings.SN_INSTANCE}/oauth_token.do"

    data = {
        "grant_type": "client_credentials",
        "client_id": settings.SN_CLIENT_ID,
        "client_secret": settings.SN_CLIENT_SECRET
    }

    r = requests.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    print("\n[OAUTH RESPONSE]", r.text)

    r.raise_for_status()

    return r.json().get("access_token")


# ---------------- HEADERS ----------------
def get_headers():

    token = get_oauth_token()

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


# ---------------- DEPLOY ----------------
def deploy_artifact(artifact):

    table = "sys_script_include"

    body = {
        "name": artifact.get("name"),
        "script": artifact.get("script"),
        "active": True
    }

    url = f"{settings.SN_INSTANCE}/api/now/table/{table}"

    r = requests.post(url, headers=get_headers(), json=body)

    print("\n[SN RESPONSE]", r.text)

    return r.json()