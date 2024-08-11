import requests
import json
import logging
import azure.functions as func
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

URL = "https://api.breezeway.io/"
CLIENT_ID = "vn7uqu3ubj9zspgz16g0fff3g553vnd7"
CLIENT_SECRET = "6wfbx65utxf2tarrkj2m4097vv3pc40j"
COMPANY_ID = 8172
fecha_hoy = ""

# Configura la zona horaria de España
def fecha():
    global fecha_hoy
    zona_horaria_españa = ZoneInfo("Europe/Madrid")

    # Obtiene la fecha y hora actuales en UTC
    fecha_hoy_utc = datetime.now(timezone.utc)

    # Convierte la fecha y hora actuales a la zona horaria de España
    fecha_hoy = fecha_hoy_utc.astimezone(zona_horaria_españa)
    # logging.info(f"Comenzando ejecución a fecha {fecha_hoy}")

    # Incrementa la fecha actual en un día
    fecha_hoy = fecha_hoy + timedelta(days=1)
    # logging.info(f"Planificando para {fecha_hoy}")

    fecha_hoy = fecha_hoy.strftime("%Y-%m-%d")

    return fecha_hoy

def conexionBreezeway():
    endpoint = URL + "public/auth/v1/"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.post(endpoint, json=payload, headers=headers)
    token = response.json().get('access_token')
    return token

def haySalidahoy(propertyID, token):
    fecha_hoy = fecha()
    endpoint = URL + f"public/inventory/v1/reservation/external-id?reference_property_id={propertyID}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'  # Asegúrate de que este es el tipo de token correcto
    }
    response = requests.get(endpoint, headers=headers)
    if response.status_code in [200, 201, 202, 204]:
        reservas = response.json()
        for reserva in reservas:
            if reserva["checkout_date"] == fecha_hoy:
                revisarPerro(reserva["reference_reservation_id"], propertyID, token)
                return True
        return False
    else:
        raise Exception(f"Error al consultar reservas: {response.status_code} - {response.text}")

def revisarPerro(idReserva, propertyID, token):
    url = f"https://api.hostaway.com/v1/financeField/{idReserva}"
    headers = {
        'Authorization': f"Bearer {token}",  # Asegúrate de que este es el tipo de token correcto
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }
    response = requests.get(url, headers=headers)
    data = response.json()['result']
    
    for element in data:
        if element['name'] == "petFee":
            marcarPerro(propertyID, token)
            return True
    return False

def marcarPerro(propertyID, token):
    fecha_hoy = fecha()  # Asegúrate de que se actualiza
    endpoint = URL + f"/public/inventory/v1/task/?reference_property_id={propertyID}&scheduled_date={fecha_hoy},{fecha_hoy}"
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    response = requests.get(endpoint, headers=headers)
    data = response.json().get('result', [])
    for element in data:
        if element["template_id"] == 101204:
            taskID = element["id"]
            nombreTarea = element["name"]
            cambiarNombreTarea(taskID, nombreTarea, token)

def cambiarNombreTarea(taskId, nombreTarea, token):
    fecha_hoy = fecha()
    nombreConPerro = "🐶" + nombreTarea 
    # logging.info(f"Moviendo tarea {taskId}")
    endpoint = URL + f"public/inventory/v1/task/{taskId}"
    headers = {'Content-Type': 'application/json', 'Authorization': f'JWT {token}'}
    payload = {"name": nombreConPerro}
    response = requests.patch(endpoint, json=payload, headers=headers)
    logging.info(f"Respuesta cambiando nombre: {response.text} {response.status_code}")
    if response.status_code in [200, 201, 202, 204]:
        return f"Tarea {taskId} cambiada nombre. {response.status_code}"
    else:
        return f"Error cambiando nombre de {taskId}: {response.status_code} {response.text}"

def conseguirPropiedades(token):
    endpoint = URL + f"public/inventory/v1/property?company_id={COMPANY_ID}&limit=350"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    response = requests.get(endpoint, headers=headers)
    return response.json()

def main(myTimer: func.TimerRequest) -> None:
    logging.info("Iniciando la función principal")
    
    # Obtener el token de autenticación
    token = conexionBreezeway()
    updates_log = []
    fecha_hoy = fecha()

    # Verificar si se obtuvo el token correctamente
    if token:
        logging.info("Token obtenido con éxito")
        
        # Obtener las propiedades
        propiedades = conseguirPropiedades(token)
        logging.info(f"Propiedades obtenidas: {len(propiedades['results'])} encontradas")
        
        # Procesar cada propiedad secuencialmente
        for propiedad in propiedades["results"]:
            propertyID = propiedad["reference_property_id"]
            
            # Verificar que la propiedad sea activa
            if propertyID is None or propiedad["status"] != "active":
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

    else:
        logging.error("Error al acceder a Breezeway")
        raise BaseException("Error al acceder a Breezeway")
