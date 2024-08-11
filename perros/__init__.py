import requests
import json
import logging
import azure.functions as func
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

URL = "https://api.breezeway.io/"
URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"  # Define la URL para obtener el token de Hostaway
CLIENT_ID = "vn7uqu3ubj9zspgz16g0fff3g553vnd7"
CLIENT_SECRET = "6wfbx65utxf2tarrkj2m4097vv3pc40j"
COMPANY_ID = 8172
fecha_hoy = ""
hostaway_token = ""  # Variable global para el token de Hostaway

# Configura la zona horaria de España
def fecha():
    global fecha_hoy
    zona_horaria_españa = ZoneInfo("Europe/Madrid")

    # Obtiene la fecha y hora actuales en UTC
    fecha_hoy_utc = datetime.now(timezone.utc)

    # Convierte la fecha y hora actuales a la zona horaria de España
    fecha_hoy = fecha_hoy_utc.astimezone(zona_horaria_españa)

    # Incrementa la fecha actual en un día
    fecha_hoy = fecha_hoy + timedelta(days=1)
    
    fecha_hoy = fecha_hoy.strftime("%Y-%m-%d")
    logging.debug(f"Fecha calculada: {fecha_hoy}")
    return fecha_hoy

def obtener_acceso_hostaway():
    global hostaway_token  # Usa la variable global
    try:
        payload = {
            "grant_type": "client_credentials",
            "client_id": "81585",
            "client_secret": "0e3c059dceb6ec1e9ec6d5c6cf4030d9c9b6e5b83d3a70d177cf66838694db5f",
            "scope": "general"
        }
        headers = {'Content-type': "application/x-www-form-urlencoded", 'Cache-control': "no-cache"}
        response = requests.post(URL_HOSTAWAY_TOKEN, data=payload, headers=headers)
        response.raise_for_status()
        hostaway_token = response.json()["access_token"]  # Almacena el token en la variable global
        logging.info("Token de Hostaway obtenido con éxito.")
    except requests.RequestException as e:
        logging.error(f"Error al obtener el token de acceso de Hostaway: {str(e)}")
        raise

def conexionBreezeway():
    endpoint = URL + "public/auth/v1/"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    headers = {
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()  # Raises HTTPError for bad responses
        token = response.json().get('access_token')
        logging.info("Conexión a Breezeway exitosa. Token obtenido.")
        return token
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conectar a Breezeway: {str(e)}")
        raise

def haySalidahoy(propertyID, token):
    fecha_hoy = fecha()
    endpoint = URL + f"public/inventory/v1/reservation/external-id?reference_property_id={propertyID}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'  # Usa el token de Breezeway proporcionado
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        reservas = response.json()
        for reserva in reservas:
            if reserva["checkout_date"] == fecha_hoy:
                revisarPerro(reserva["reference_reservation_id"], propertyID)
                logging.info(f"Reserva con salida hoy encontrada: {reserva}")
                return True
        logging.info(f"No hay reservas con salida para hoy en la propiedad {propertyID}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al consultar reservas para propiedad {propertyID}: {str(e)}")
        raise

def revisarPerro(idReserva, propertyID):
    global hostaway_token  # Usa la variable global para el token de Hostaway
    url = f"https://api.hostaway.com/v1/financeField/{idReserva}"
    headers = {
        'Authorization': f"Bearer {hostaway_token}",  # Usa la variable global
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json().get('result', [])
        for element in data:
            if element['name'] == "petFee":
                logging.info(f"Pet fee encontrado para reserva {idReserva}")
                marcarPerro(propertyID, token)
                return True
        logging.info(f"No se encontró pet fee para reserva {idReserva}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al revisar perro para reserva {idReserva}: {str(e)}")
        raise

def marcarPerro(propertyID, token):
    fecha_hoy = fecha()  # Asegúrate de que se actualiza
    endpoint = URL + f"/public/inventory/v1/task/?reference_property_id={propertyID}&scheduled_date={fecha_hoy},{fecha_hoy}"
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        data = response.json().get('result', [])
        for element in data:
            if element["template_id"] == 101204:
                taskID = element["id"]
                nombreTarea = element["name"]
                cambiarNombreTarea(taskID, nombreTarea, token)
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al marcar perro para propiedad {propertyID}: {str(e)}")
        raise

def cambiarNombreTarea(taskId, nombreTarea, token):
    fecha_hoy = fecha()
    nombreConPerro = "🐶" + nombreTarea 
    endpoint = URL + f"public/inventory/v1/task/{taskId}"
    headers = {'Content-Type': 'application/json', 'Authorization': f'JWT {token}'}
    payload = {"name": nombreConPerro}
    try:
        response = requests.patch(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        logging.info(f"Tarea {taskId} cambiada a {nombreConPerro} exitosamente.")
        return f"Tarea {taskId} cambiada nombre. {response.status_code}"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error cambiando nombre de tarea {taskId}: {str(e)}")
        raise

def conseguirPropiedades(token):
    endpoint = URL + f"public/inventory/v1/property?company_id={COMPANY_ID}&limit=350"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        logging.info("Propiedades obtenidas con éxito.")
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conseguir propiedades: {str(e)}")
        raise

def main(myTimer: func.TimerRequest) -> None:
    global hostaway_token  # Usa la variable global para el token de Hostaway
    logging.info("Iniciando la función principal")
    
    # Obtener el token de autenticación de Hostaway
    try:
        obtener_acceso_hostaway()  # Obtiene y almacena el token en la variable global
        token = conexionBreezeway()  # Obtiene el token de Breezeway
        updates_log = []
        fecha_hoy = fecha()

        # Obtener las propiedades
        propiedades = conseguirPropiedades(token)
        logging.info(f"Propiedades obtenidas: {len(propiedades['results'])} encontradas")
        
        # Procesar cada propiedad secuencialmente
        for propiedad in propiedades["results"]:
            propertyID = propiedad["reference_property_id"]
            
            # Verificar que la propiedad sea activa
            if propertyID is None or propiedad["status"] != "active":
                logging.debug(f"Propiedad {propertyID} inactiva o no válida.")
                continue
            
            # Comprobar si hay salida hoy
            try:
                if haySalidahoy(propertyID, token):
                    logging.info(f"Salida encontrada para la propiedad {propertyID}")
                else:
                    logging.info(f"No hay salida hoy para la propiedad {propertyID}")
            except Exception as e:
                logging.error(f"Error procesando propiedad {propertyID}: {str(e)}")
                updates_log.append(f"Error en {propertyID}: {str(e)}")

    except Exception as e:
        logging.error(f"Error general: {str(e)}")
        raise BaseException("Error al acceder a los servicios")