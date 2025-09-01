import requests
import json
import logging
import azure.functions as func
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

URL = "https://api.breezeway.io/"
URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"
CLIENT_ID = "vn7uqu3ubj9zspgz16g0fff3g553vnd7"
CLIENT_SECRET = "6wfbx65utxf2tarrkj2m4097vv3pc40j"
COMPANY_ID = 8172

# Variables globales (ojo a su uso en Azure Functions)
fecha_hoy = ""
hostaway_token = ""  # Token de Hostaway

HTTP_TIMEOUT = (5, 25)  # (conexión, lectura)

def fecha():
    """
    Respeta tu comportamiento original:
    - Ahora UTC -> Europe/Madrid -> +1 día -> 'YYYY-MM-DD'
    """
    global fecha_hoy
    zona_horaria_españa = ZoneInfo("Europe/Madrid")
    fecha_hoy_utc = datetime.now(timezone.utc)
    fecha_local = fecha_hoy_utc.astimezone(zona_horaria_españa)
    fecha_local = fecha_local + timedelta(days=1)  # +1 día (NO tocar)
    fecha_hoy = fecha_local.strftime("%Y-%m-%d")
    logging.debug(f"Fecha calculada (Madrid +1 día): {fecha_hoy}")
    return fecha_hoy

def obtener_acceso_hostaway():
    global hostaway_token
    try:
        payload = {
            "grant_type": "client_credentials",
            "client_id": "81585",
            "client_secret": "0e3c059dceb6ec1e9ec6d5c6cf4030d9c9b6e5b83d3a70d177cf66838694db5f",
            "scope": "general"
        }
        headers = {
            'Content-type': "application/x-www-form-urlencoded",
            'Cache-control': "no-cache"
        }
        response = requests.post(URL_HOSTAWAY_TOKEN, data=payload, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        hostaway_token = response.json()["access_token"]
        logging.info("Token de Hostaway obtenido con éxito.")
    except requests.RequestException as e:
        logging.error(f"Error al obtener el token de acceso de Hostaway: {str(e)}")
        raise

def conexionBreezeway():
    endpoint = URL.rstrip("/") + "/public/auth/v1/"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    headers = {
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        token = response.json().get('access_token')
        if not token:
            raise RuntimeError("Respuesta de auth Breezeway sin access_token")
        logging.info("Conexión a Breezeway exitosa. Token obtenido.")
        return token
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conectar a Breezeway: {str(e)}")
        raise

def nombre_principal(reserva: dict) -> str:
    guests = reserva.get("guests") or []
    if not guests:
        return ""
    first = (guests[0].get("first_name") or "").strip()
    last = (guests[0].get("last_name") or "").strip()
    return (first + " " + last).strip()

def haySalidahoy(propertyID, token):
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/reservation/external-id?reference_property_id={propertyID}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        reservas = response.json().get('results', [])
        for reserva in reservas:
            if reserva.get("checkout_date") == fecha_target:
                nombreCliente = nombre_principal(reserva)
                revisarPerro(reserva.get("reference_reservation_id"), propertyID, token, nombreCliente)
                logging.info(f"Reserva con salida hoy encontrada: {reserva.get('reference_reservation_id')}")
                return True
        logging.info(f"No hay reservas con salida para hoy en la propiedad {propertyID}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al consultar reservas para propiedad {propertyID}: {str(e)}")
        raise

def hayEntradaHoy(propertyID, token):
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/reservation/external-id?reference_property_id={propertyID}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        reservas = response.json().get('results', [])
        for reserva in reservas:
            if reserva.get("checkin_date") == fecha_target:
                nombreCliente = nombre_principal(reserva)
                revisarCuna(reserva.get("reference_reservation_id"), propertyID, token, nombreCliente)
                logging.info(f"Reserva con entrada hoy encontrada: {reserva.get('reference_reservation_id')}")
                return True
        logging.info(f"No hay reservas con entrada para hoy en la propiedad {propertyID}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al consultar reservas para propiedad {propertyID}: {str(e)}")
        raise

def revisarCuna(idReserva, propertyID, token, nombreCliente):
    global hostaway_token
    url = f"https://api.hostaway.com/v1/financeField/{idReserva}"
    headers = {
        'Authorization': f"Bearer {hostaway_token}",
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }
    try:
        response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json().get('result', [])
        for element in data:
            alias = (element.get('alias') or "").lower()
            name = (element.get('name') or "").lower()
            if alias == "cuna" or "cuna" in name:
                logging.info(f"Cuna encontrada para reserva {idReserva}")
                marcarCuna(propertyID, token, nombreCliente)
                return True
        logging.info(f"No se encontró cuna para reserva {idReserva}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al revisar Cuna para reserva {idReserva}: {str(e)}")
        raise

def revisarPerro(idReserva, propertyID, token, nombreCliente):
    global hostaway_token
    url = f"https://api.hostaway.com/v1/financeField/{idReserva}"
    headers = {
        'Authorization': f"Bearer {hostaway_token}",
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }
    try:
        response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json().get('result', [])
        for element in data:
            alias = (element.get('alias') or "").lower()
            name = (element.get('name') or "").lower()
            if alias == "petfee" or "pet fee" in name or name == "petfee":
                logging.info(f"Pet fee encontrado para reserva {idReserva}")
                marcarPerro(propertyID, token, nombreCliente)
                return True
        logging.info(f"No se encontró pet fee para reserva {idReserva}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al revisar perro para reserva {idReserva}: {str(e)}")
        raise

def marcarCuna(propertyID, token, nombreCliente):
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + "/public/inventory/v1/task/"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        'Authorization': f'JWT {token}'
    }
    nombre = f"Llevar Cuna {nombreCliente}".strip()
    payload = {
        "rate_type": "piece",
        "assign_default_workers": False,
        "reference_property_id": propertyID,   # (no set)
        "name": nombre,
        "scheduled_date": fecha_target
    }
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        logging.info(f"Tarea creada: {nombre} para {fecha_target} en prop {propertyID}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al crear tarea de cuna para propiedad {propertyID}: {str(e)}")
        raise

def marcarPerro(propertyID, token, nombreCliente):
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/task/?reference_property_id={propertyID}&scheduled_date={fecha_target},{fecha_target}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json().get('results', [])
        for element in data:
            # Ajusta el template_id si procede
            if element.get("template_id") == 101204:
                taskID = element["id"]
                nombreTarea = element.get("name", "")
                cambiarNombreTarea(taskID, nombreTarea, token, nombreCliente)
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al marcar perro para propiedad {propertyID}: {str(e)}")
        raise

def cambiarNombreTarea(taskId, nombreTarea, token, nombreCliente):
    nombreTarea = nombreTarea or ""
    if "(Perro)" not in nombreTarea:
        nombreConPerro = f"{nombreTarea} (Perro)"
    else:
        nombreConPerro = nombreTarea  # idempotente

    endpoint = URL.rstrip("/") + f"/public/inventory/v1/task/{taskId}"
    headers = {'Content-Type': 'application/json', 'Authorization': f'JWT {token}'}
    payload = {"name": nombreConPerro, "description": f"Cliente: {nombreCliente}"}
    try:
        response = requests.patch(endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        logging.info(f"Tarea {taskId} renombrada a '{nombreConPerro}' con descripción del cliente.")
        return f"Tarea {taskId} renombrada. {response.status_code}"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error cambiando nombre de tarea {taskId}: {str(e)}")
        raise

def conseguirPropiedades(token):
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/property?company_id={COMPANY_ID}&limit=350"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        raw = data.get("results", data if isinstance(data, list) else [])

        # Normaliza a lista de dicts (aplana si vienen sub-listas)
        props = []
        if isinstance(raw, dict):
            props = [raw]
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    props.append(item)
                elif isinstance(item, list):
                    props.extend([i for i in item if isinstance(i, dict)])

        logging.info(f"Propiedades obtenidas con éxito: {len(props)}")
        return props
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conseguir propiedades: {str(e)}")
        raise

def main(myTimer: func.TimerRequest) -> None:
    global hostaway_token
    logging.info("Iniciando la función principal")

    try:
        # Tokens
        obtener_acceso_hostaway()
        token_breezeway = conexionBreezeway()

        # Forzar cálculo de fecha (Madrid +1 día) para logging
        _ = fecha()

        # Propiedades
        propiedades = conseguirPropiedades(token_breezeway)
        logging.info(f"Propiedades obtenidas: {len(propiedades)} encontradas")

        # Procesar propiedades
        for propiedad in propiedades:
            propertyID = propiedad.get("reference_property_id")
            if propertyID is None or propiedad.get("status") != "active":
                logging.debug(f"Propiedad {propertyID} inactiva o no válida.")
                continue

            try:
                salida = haySalidahoy(propertyID, token_breezeway)
                entrada = hayEntradaHoy(propertyID, token_breezeway)

                if salida:
                    logging.info(f"Salida encontrada para la propiedad {propertyID}")
                if entrada:
                    logging.info(f"Entrada encontrada para la propiedad {propertyID}")
                if not (salida or entrada):
                    logging.info(f"No hay salida ni entrada hoy para la propiedad {propertyID}")

            except Exception as e:
                logging.error(f"Error en propiedad {propertyID}: {str(e)}")

    except Exception as e:
        logging.error(f"Error general: {str(e)}")
        raise BaseException("Error al acceder a los servicios")