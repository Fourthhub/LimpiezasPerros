import requests
import json
import logging
import os
import azure.functions as func
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = "https://api.breezeway.io/"
URL_HOSTAWAY_TOKEN = "https://api.hostaway.com/v1/accessTokens"
CLIENT_ID = os.environ["breezeway_client_id"]
CLIENT_SECRET = os.environ["breezeway_client_secret"]
COMPANY_ID = 8172
TEMPLATE_ID_LIMPIEZA_GENERAL = 101204

# Variables globales (ojo a su uso en Azure Functions)
fecha_hoy = ""
hostaway_token = ""  # Token de Hostaway

HTTP_TIMEOUT = (5, 25)  # (conexión, lectura)

# --- Helpers mínimos para normalizar JSON ---
def _results(obj):
    # Devuelve obj["results"] si existe; si no, devuelve el propio obj (lista ya utilizable)
    return obj["results"] if isinstance(obj, dict) and "results" in obj else obj

def _result(obj):
    # Igual, pero para claves 'result'
    return obj["result"] if isinstance(obj, dict) and "result" in obj else obj

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
    # Se llama desde el hilo principal antes de lanzar los workers: el logging
    # aqui si se ve reflejado en Application Insights (ver nota en main()).
    global hostaway_token
    try:
        payload = {
            "grant_type": "client_credentials",
            "client_id": os.environ["hostaway_client_id"],
            "client_secret": os.environ["hostaway_client_secret"],
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

def reservasDeLaPropiedad(propertyID, token):
    """Trae UNA vez el listado de reservas de una propiedad (antes se pedia
    dos veces: una para mirar salidas y otra para entradas).

    Descarta los bloqueos de disponibilidad (type_reservation "hold"), que
    Breezeway devuelve mezclados en la misma lista con un
    reference_reservation_id que en realidad es el rango de fechas del bloqueo
    (p.ej. "2026-09-07_2027-03-05") en vez de un ID de reserva de Hostaway. Si
    su fecha de inicio/fin coincidia con la del dia, antes se trataban como una
    entrada/salida real y la consulta a financeField fallaba con 404.

    Filtramos exigiendo que el id sea numerico, que es justo la precondicion
    para que la llamada a Hostaway tenga sentido. Comprobado sobre las 887
    reservas reales: los 649 "booking" tienen id numerico y los 238 "hold" no,
    sin un solo caso mal clasificado. (Ojo: NO vale filtrar por "guests"
    vacio, porque hay 12 reservas reales sin datos de huesped que se
    perderian.)
    """
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/reservation/external-id?reference_property_id={propertyID}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    reservas = _results(response.json())  # <--- normaliza
    return [r for r in reservas if str(r.get("reference_reservation_id") or "").isdigit()]

def cargosDeLaReserva(idReserva):
    """Los financeField de una reserva. Se pide una sola vez por reserva: en una
    salida hay que mirar dos cargos distintos (pet fee y cuna) y antes eso
    suponia dos peticiones identicas a Hostaway."""
    global hostaway_token
    url = f"https://api.hostaway.com/v1/financeField/{idReserva}"
    headers = {
        'Authorization': f"Bearer {hostaway_token}",
        'Content-type': "application/json",
        'Cache-control': "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return _result(response.json()) or []  # <--- normaliza

def _tiene_cuna(cargos):
    # En produccion el cargo llega como name='otherFees' con alias='Cuna'.
    for element in cargos:
        alias = (element.get('alias') or "").lower()
        name = (element.get('name') or "").lower()
        if alias == "cuna" or "cuna" in name:
            return True
    return False

def _tiene_petfee(cargos):
    # En produccion el cargo llega como name='petFee' con alias='Mascota'.
    for element in cargos:
        alias = (element.get('alias') or "").lower()
        name = (element.get('name') or "").lower()
        if alias == "petfee" or "pet fee" in name or name == "petfee":
            return True
    return False

def marcarCuna(propertyID, token, nombreCliente, idReservaBreezeway=None, accion="Llevar"):
    """Crea la tarea de cuna del dia y, si conocemos el id interno de la reserva
    en Breezeway, la deja vinculada a ella (así la tarea acompaña a la reserva
    en vez de quedar suelta en la propiedad).

    'accion' es "Llevar" en la entrada y "Recoger" en la salida.

    Devuelve (taskId, vinculada)."""
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + "/public/inventory/v1/task/"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        'Authorization': f'JWT {token}'
    }
    nombre = f"{accion} Cuna {nombreCliente}".strip()
    payload = {
        "rate_type": "piece",
        "assign_default_workers": False,
        "reference_property_id": propertyID,
        "name": nombre,
        "scheduled_date": fecha_target,
        # OBLIGATORIO aunque la documentacion lo marque como opcional: sin este
        # campo el API responde 422 "Missing data for required field". Es la
        # razon por la que esta funcion no ha creado ni una sola tarea desde que
        # existe (ago-2025): fallaba siempre, y el error se perdia en el hilo.
        "type_department": "housekeeping",
    }
    response = requests.post(endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()

    creada = _result(response.json())
    if isinstance(creada, list):
        creada = creada[0] if creada else {}
    taskId = (creada or {}).get("id")

    # El endpoint de crear tarea no admite vincular la reserva; hay que hacerlo
    # con una segunda llamada (POST /reservation/{id}/tasks), y usa el id INTERNO
    # de Breezeway, no el reference_reservation_id de Hostaway.
    # Si la vinculacion falla no relanzamos: la tarea ya esta creada, que es lo
    # que de verdad importa para la limpieza. Se devuelve vinculada=False para
    # que el hilo principal lo registre como aviso, no como un fallo total.
    vinculada = False
    if taskId and idReservaBreezeway:
        try:
            link_endpoint = URL.rstrip("/") + f"/public/inventory/v1/reservation/{idReservaBreezeway}/tasks"
            link_resp = requests.post(link_endpoint, json={"task_id": taskId}, headers=headers, timeout=HTTP_TIMEOUT)
            link_resp.raise_for_status()
            vinculada = True
        except requests.exceptions.RequestException:
            vinculada = False

    return taskId, vinculada

def marcarPerro(propertyID, token, nombreCliente):
    """Busca la tarea de limpieza general del dia (template Limpieza General)
    y le añade '(Perro)'. Devuelve True si encontro una tarea y la renombro,
    False si no habia ninguna tarea con ese template todavia."""
    fecha_target = fecha()
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/task/?reference_property_id={propertyID}&scheduled_date={fecha_target},{fecha_target}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    tareas = _results(response.json())  # <--- normaliza
    marcada = False
    for element in tareas:
        if element.get("template_id") == TEMPLATE_ID_LIMPIEZA_GENERAL:
            taskID = element["id"]
            nombreTarea = element.get("name", "")
            cambiarNombreTarea(taskID, nombreTarea, token, nombreCliente)
            marcada = True
    return marcada

def cambiarNombreTarea(taskId, nombreTarea, token, nombreCliente):
    nombreTarea = nombreTarea or ""
    if "(Perro)" not in nombreTarea:
        nombreConPerro = f"{nombreTarea} (Perro)"
    else:
        nombreConPerro = nombreTarea  # idempotente

    endpoint = URL.rstrip("/") + f"/public/inventory/v1/task/{taskId}"
    headers = {'Content-Type': 'application/json', 'Authorization': f'JWT {token}'}
    payload = {"name": nombreConPerro, "description": f"Cliente: {nombreCliente}"}
    response = requests.patch(endpoint, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    response.raise_for_status()

def conseguirPropiedades(token):
    endpoint = URL.rstrip("/") + f"/public/inventory/v1/property?company_id={COMPANY_ID}&limit=350"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'JWT {token}'
    }
    try:
        response = requests.get(endpoint, headers=headers, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        props = _results(response.json())  # <--- normaliza
        # Asegura que iteramos dicts (por si viniera alguna sublista)
        propiedades = []
        if isinstance(props, dict):
            propiedades = [props]
        elif isinstance(props, list):
            for it in props:
                if isinstance(it, dict):
                    propiedades.append(it)
                elif isinstance(it, list):
                    propiedades.extend([i for i in it if isinstance(i, dict)])

        if len(propiedades) >= 350:
            logging.warning(
                f"conseguirPropiedades devolvió {len(propiedades)} propiedades, "
                f"al límite del 'limit=350' de la petición: podría haber más sin traer."
            )

        logging.info(f"Propiedades obtenidas con éxito: {len(propiedades)}")
        return propiedades
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conseguir propiedades: {str(e)}")
        raise

def procesar(propiedad, token_breezeway):
    """Se ejecuta en un hilo del ThreadPoolExecutor. Todo lo que hace (y
    cualquier fallo puntual con una reserva) se devuelve en el diccionario de
    resultado en vez de loguearse aqui, porque el logging emitido dentro de
    estos hilos no llega a Application Insights (verificado: en 30 dias y
    ~9000 comprobaciones, cero logs de estas funciones sobrevivieron, frente
    a miles logueados desde el hilo principal). El hilo principal es quien
    interpreta este resultado y lo loguea."""
    propertyID = propiedad["reference_property_id"]
    fecha_target = fecha()
    resultado = {
        "propertyID": propertyID,
        "salidas": [],
        "entradas": [],
        "error_general": None,
    }
    try:
        reservas = reservasDeLaPropiedad(propertyID, token_breezeway)
    except Exception as e:
        resultado["error_general"] = str(e)
        return resultado

    for reserva in reservas:
        idReserva = reserva.get("reference_reservation_id")
        nombreCliente = nombre_principal(reserva)

        if reserva.get("checkout_date") == fecha_target:
            # En la salida miramos dos cosas sobre los MISMOS cargos: si hubo
            # perro (se marca la limpieza) y si hubo cuna (hay que recogerla).
            evento = {"idReserva": idReserva}
            try:
                cargos = cargosDeLaReserva(idReserva)
                evento["petfee"] = _tiene_petfee(cargos)
                if evento["petfee"]:
                    evento["tarea_marcada"] = marcarPerro(propertyID, token_breezeway, nombreCliente)
                evento["cuna"] = _tiene_cuna(cargos)
                if evento["cuna"]:
                    _taskId, evento["vinculada"] = marcarCuna(
                        propertyID, token_breezeway, nombreCliente, reserva.get("id"), accion="Recoger"
                    )
            except Exception as e:
                evento["error"] = str(e)
            resultado["salidas"].append(evento)

        if reserva.get("checkin_date") == fecha_target:
            evento = {"idReserva": idReserva}
            try:
                cargos = cargosDeLaReserva(idReserva)
                evento["cuna"] = _tiene_cuna(cargos)
                if evento["cuna"]:
                    _taskId, evento["vinculada"] = marcarCuna(
                        propertyID, token_breezeway, nombreCliente, reserva.get("id"), accion="Llevar"
                    )
            except Exception as e:
                evento["error"] = str(e)
            resultado["entradas"].append(evento)

    return resultado

def main(myTimer: func.TimerRequest) -> None:
    global hostaway_token
    logging.info("Iniciando la función principal")

    try:
        # Tokens
        obtener_acceso_hostaway()
        token_breezeway = conexionBreezeway()

        # Forzar cálculo de fecha (Madrid +1 día) para logging
        fecha_target = fecha()
        logging.info(f"Comprobando entradas/salidas para: {fecha_target}")

        # Propiedades
        propiedades = conseguirPropiedades(token_breezeway)
        logging.info(f"Propiedades obtenidas: {len(propiedades)} encontradas")

        propiedades_activas = [p for p in propiedades if p["status"] == "active"]

        # Algunas propiedades activas no son unidades reservables (oficinas,
        # parking, la vivienda del propietario, o un registro agregado de
        # "hotel") y no tienen reference_property_id. Antes se les preguntaba
        # igualmente a Breezeway y siempre devolvía 422; se omiten aquí para
        # no generar ese ruido cada día.
        sin_id = [p for p in propiedades_activas if not p.get("reference_property_id")]
        if sin_id:
            nombres = ", ".join(p.get("name") or p.get("display_name") or "?" for p in sin_id)
            logging.info(f"{len(sin_id)} propiedades activas sin reference_property_id, se omiten: {nombres}")
        propiedades_activas = [p for p in propiedades_activas if p.get("reference_property_id")]

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(procesar, p, token_breezeway): p["reference_property_id"]
                for p in propiedades_activas
            }

            for future in as_completed(futures):
                propertyID = futures[future]
                try:
                    resultado = future.result()
                except Exception as e:
                    logging.error(f"Error en propiedad {propertyID}: {str(e)}")
                    continue

                if resultado["error_general"]:
                    logging.error(f"Error en propiedad {propertyID}: {resultado['error_general']}")
                    continue

                for salida in resultado["salidas"]:
                    ref = f"Propiedad {propertyID}, salida de la reserva {salida['idReserva']}"
                    if "error" in salida:
                        logging.error(f"{ref}: error al revisarla: {salida['error']}")
                        continue

                    if salida.get("petfee") and salida.get("tarea_marcada"):
                        logging.info(f"{ref}: con pet fee, tarea de limpieza marcada.")
                    elif salida.get("petfee"):
                        logging.warning(
                            f"{ref}: pet fee encontrado pero no había ninguna tarea de limpieza "
                            f"que marcar (¿aún no creada en Breezeway?)."
                        )

                    if salida.get("cuna") and salida.get("vinculada"):
                        logging.info(f"{ref}: con cuna, tarea de recogida creada y vinculada a la reserva.")
                    elif salida.get("cuna"):
                        logging.warning(f"{ref}: con cuna, tarea de recogida creada pero SIN vincular a la reserva.")

                    if not salida.get("petfee") and not salida.get("cuna"):
                        logging.info(f"{ref}: sin pet fee ni cuna.")

                for entrada in resultado["entradas"]:
                    ref = f"Propiedad {propertyID}, entrada de la reserva {entrada['idReserva']}"
                    if "error" in entrada:
                        logging.error(f"{ref}: error al revisarla: {entrada['error']}")
                    elif entrada.get("cuna") and entrada.get("vinculada"):
                        logging.info(f"{ref}: con cuna, tarea de entrega creada y vinculada a la reserva.")
                    elif entrada.get("cuna"):
                        logging.warning(f"{ref}: con cuna, tarea de entrega creada pero SIN vincular a la reserva.")
                    else:
                        logging.info(f"{ref}: sin cuna.")

                if not resultado["salidas"] and not resultado["entradas"]:
                    logging.info(f"No hay salida ni entrada hoy para la propiedad {propertyID}")

    except Exception as e:
        logging.error(f"Error general: {str(e)}")
        raise BaseException("Error al acceder a los servicios")
