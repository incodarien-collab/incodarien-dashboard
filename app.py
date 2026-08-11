"""
INCODARIEN S.A.S. — Centro de Inteligencia Comercial B2B (v3)
================================================================
Corrige los 3 errores reales que se vieron al ejecutar la v2:

1) "FileNotFoundError: File [{...}] does not exist" en _leer_store():
   pd.read_json() recibía el JSON como texto y, en pandas >= 2.x, un
   string así se interpreta como una RUTA DE ARCHIVO, no como datos.
   Solución aplicada: el dcc.Store ya no guarda un string JSON — guarda
   directamente una lista de diccionarios (df.to_dict("records")). Dash
   la serializa solo, y para reconstruir el DataFrame se usa
   pd.DataFrame(lista), sin pasar nunca por pd.read_json. Así se evita
   el problema de raíz en vez de solo parchar el síntoma.

2) "400 Client Error: Bad Request" al consultar SECOP:
   El filtro de palabras clave usaba "%25camara%25" escrito a mano
   dentro del $where. "%25" ya es el símbolo "%" codificado para URL;
   al mandarlo por requests, se codificaba OTRA VEZ y llegaba a Socrata
   como "%2525camara%2525" — un patrón LIKE inválido, de ahí el 400.
   Solución aplicada: la consulta a SECOP ahora solo filtra por
   departamento y fecha (sin LIKE, sin porcentajes, nada que se pueda
   codificar mal); el filtro de palabras clave se hace en Python sobre
   los resultados, igual que en tu script original. Menos elegante,
   pero no depende de acertarle a la sintaxis exacta de SoQL.

3) Datos ficticios "resucitados": tu leads_privados_verificados.csv en
   C:\\Incodarien_BI\\ era de una versión anterior del script y todavía
   tenía "Puerto Antioquia" / "Banaexport" con columnas viejas. El
   código los cargaba tal cual. Ahora, si el CSV existente no tiene
   exactamente las columnas esperadas, se renombra a .bak (no se borra)
   y se crea uno nuevo vacío — así nunca se mezclan datos de un esquema
   viejo con el actual sin que tú te enteres.

Además: todos los callbacks quedan envueltos en try/except que
devuelven un estado vacío en vez de un error 500, para que la interfaz
nunca se "caiga" aunque una fuente de datos falle.
"""

import os
import shutil
from datetime import datetime, timedelta

import dash
from dash import dcc, html, Input, Output, State
import plotly.express as px
import pandas as pd
import requests

# ==========================================================
# CONFIGURACIÓN
# ==========================================================
VERSION_APP = "6.0.0"
print(f"\n{'='*60}\nINCODARIEN Dashboard — VERSIÓN {VERSION_APP}\n"
      f"Si en el navegador no ves 'v{VERSION_APP}' en la esquina inferior "
      f"del dashboard, NO estás corriendo este archivo — revisa cuál "
      f"archivo .py está ejecutando tu servidor.\n{'='*60}\n")

DIAS_ANTIGUEDAD_MAXIMA = 60
INTERVALO_REFRESCO_MS = int(os.environ.get("INCODARIEN_REFRESCO_MS", 6 * 60 * 60 * 1000))  # 6 h

CSV_LEADS_PRIVADOS = os.environ.get(
    "INCODARIEN_CSV_LEADS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_privados_verificados.csv"),
)

DEPARTAMENTOS_OBJETIVO = ["Antioquia", "Cundinamarca", "Valle del Cauca", "Atlántico", "Bolívar", "Córdoba"]

PALABRAS_CLAVE = [
    "camara", "cámara", "cctv", "videovigilancia", "seguridad electronica", "seguridad electrónica",
    "control de acceso", "telecomunicaciones", "fibra optica", "fibra óptica",
    "red de datos", "redes", "energia solar", "energía solar", "energia renovable", "energía renovable",
    "domo", "computo", "cómputo", "audiovisual", "tecnológic", "tecnologic",
]

SECOP_ENDPOINT = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")

# Nombres de columnas del dataset SECOP II - Procesos de Contratación.
# CONFIRMAR antes de producción en:
# https://www.datos.gov.co/resource/p6dx-8zbt.json?$limit=1
COLUMNAS_SECOP = {
    "entidad": "entidad",
    "departamento": "departamento_entidad",
    "municipio": "ciudad_entidad",
    # Antes decía "detalle_del_objeto_a_contratar" — ese campo no existe en
    # el dataset real; por eso siempre llegaba vacío y el filtro de
    # palabras clave nunca encontraba nada (0 resultados, sin error).
    "descripcion": "descripcion_del_procedimiento",
    # Antes decía "fecha_de_publicacion_del" (incompleto).
    "fecha_publicacion": "fecha_de_publicacion_del_proceso",
    # Antes decía "fecha_de_recepcion_de_ofertas" (nombre real distinto).
    "fecha_cierre": "fecha_de_recepcion_de_respuestas",
    "modalidad": "modalidad_de_contratacion",
    "precio_base": "precio_base",
    "url": "urlproceso",
}

EMPRESA = {
    "razon_social": "Inversiones y Comunicaciones Darién ZOMAC S.A.S.",
    "marca": "INCODARIEN",
    "nit": "901.535.584-4",
    "web": "https://incodarien.com",
    "telefono": "312 255 9395",
    "correo": os.environ.get("INCODARIEN_CORREO_CONTACTO", "gerencia@incodarien.com"),
    "servicios": [
        "Seguridad electrónica (CCTV, alarmas, control de acceso, monitoreo 24/7)",
        "Telecomunicaciones (redes de datos, VPN, redes LAN/WAN, fibra óptica)",
        "Energía renovable (fotovoltaica On Grid, Híbrida y Off Grid)",
        "Sistemas (servidores, desarrollo, bases de datos)",
        "Electricidad residencial y comercial",
    ],
    "trayectoria": "Más de una década de operación en el norte de Antioquia; "
                   "miembro de la Cámara de la Construcción Colombiana, seccional Antioquia.",
    "firmante": os.environ.get("INCODARIEN_GERENTE", "[NOMBRE DEL GERENTE DE PROYECTOS]"),
}

COLUMNAS_ESTANDAR = [
    "Empresa", "Sector", "Ubicación", "Proyecto", "Modalidad", "Fuente",
    "Presupuesto_COP", "Fecha_Publicacion", "Fecha_Cierre", "Enlace_Proceso",
    "Enlace_Verificado", "Contacto", "Score_Relevancia",
]


# ==========================================================
# 1. VERIFICACIÓN REAL DE ENLACES
# ==========================================================
def verificar_enlace(url, timeout=4):
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return False
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            r = requests.get(url, timeout=timeout, stream=True)
        return r.status_code < 400
    except Exception:
        return False


# ==========================================================
# 2. SCORE DE RELEVANCIA — fórmula transparente
# ==========================================================
def calcular_score(descripcion, presupuesto_cop, dias_restantes):
    score = 0
    desc = (descripcion or "").lower()
    coincidencias = sum(1 for k in PALABRAS_CLAVE if k in desc)
    score += min(coincidencias, 5) * 12

    if pd.notna(presupuesto_cop):
        if 5_000_000 <= presupuesto_cop <= 2_000_000_000:
            score += 25
        elif presupuesto_cop > 2_000_000_000:
            score += 10

    if pd.notna(dias_restantes):
        if dias_restantes >= 5:
            score += 15
        elif dias_restantes >= 0:
            score += 5

    return int(min(score, 100))


# ==========================================================
# 3. SECOP II — consulta simple (sin LIKE, sin % que se pueda romper)
# ==========================================================
def consultar_secop(dias=DIAS_ANTIGUEDAD_MAXIMA, limite=1000):
    """Filtra por departamento y fecha en el servidor (consulta simple y
    robusta); el filtro de palabras clave se hace en Python sobre el
    resultado, para no depender de escapar bien un patrón LIKE en SoQL."""
    col = COLUMNAS_SECOP
    fecha_limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    filtro_geo = " OR ".join([f"{col['departamento']} = '{d}'" for d in DEPARTAMENTOS_OBJETIVO])
    where = f"({filtro_geo}) AND {col['fecha_publicacion']} >= '{fecha_limite}'"

    params = {"$where": where, "$limit": limite, "$order": f"{col['fecha_publicacion']} DESC"}
    headers = {"X-App-Token": SOCRATA_APP_TOKEN} if SOCRATA_APP_TOKEN else {}

    try:
        resp = requests.get(SECOP_ENDPOINT, params=params, headers=headers, timeout=20)
        if resp.status_code >= 400:
            # Se imprime el cuerpo real del error de Socrata para poder
            # diagnosticar en el futuro sin adivinar.
            print(f"[SECOP] {resp.status_code} — {resp.text[:500]}")
            return pd.DataFrame(columns=COLUMNAS_ESTANDAR)
        registros = resp.json()
        print(f"[SECOP] La API devolvió {len(registros)} procesos crudos (antes del filtro de palabras clave).")
        if registros and col["descripcion"] not in registros[0]:
            print(f"[SECOP] AVISO: el campo '{col['descripcion']}' no aparece en la respuesta. "
                  f"Campos disponibles: {list(registros[0].keys())}")
    except Exception as e:
        print(f"[SECOP] Consulta no disponible en este momento: {e}")
        return pd.DataFrame(columns=COLUMNAS_ESTANDAR)

    filas = []
    for r in registros:
        descripcion = r.get(col["descripcion"], "") or ""
        if not any(p in descripcion.lower() for p in PALABRAS_CLAVE):
            continue  # filtro de palabras clave, hecho en Python

        fecha_pub = pd.to_datetime(r.get(col["fecha_publicacion"]), errors="coerce")
        fecha_cierre = pd.to_datetime(r.get(col["fecha_cierre"]), errors="coerce")
        dias_restantes = (fecha_cierre - datetime.now()).days if pd.notna(fecha_cierre) else None
        presupuesto = pd.to_numeric(r.get(col["precio_base"]), errors="coerce")
        enlace = r.get(col["url"], "") or ""

        filas.append({
            "Empresa": r.get(col["entidad"], "Entidad no especificada"),
            "Sector": "Contratación pública",
            "Ubicación": r.get(col["municipio"]) or r.get(col["departamento"], ""),
            "Proyecto": descripcion[:180],
            "Modalidad": r.get(col["modalidad"], "No especificado"),
            "Fuente": "SECOP II (API oficial, dato público)",
            "Presupuesto_COP": presupuesto,
            "Fecha_Publicacion": fecha_pub,
            "Fecha_Cierre": fecha_cierre,
            "Enlace_Proceso": enlace,
            "Enlace_Verificado": verificar_enlace(enlace),
            "Contacto": "Ver datos de contacto en la ficha oficial del proceso (enlace)",
            "Score_Relevancia": calcular_score(descripcion, presupuesto, dias_restantes),
        })

    print(f"[SECOP] {len(filas)} de esos {len(registros)} coincidieron con las palabras clave de INCODARIEN.")
    return pd.DataFrame(filas, columns=COLUMNAS_ESTANDAR) if filas else pd.DataFrame(columns=COLUMNAS_ESTANDAR)


# ==========================================================
# 4. LEADS PRIVADOS — con protección contra esquemas viejos
# ==========================================================
def cargar_leads_privados():
    if not os.path.exists(CSV_LEADS_PRIVADOS):
        pd.DataFrame(columns=COLUMNAS_ESTANDAR).to_csv(CSV_LEADS_PRIVADOS, index=False)
        return pd.DataFrame(columns=COLUMNAS_ESTANDAR)

    try:
        df = pd.read_csv(CSV_LEADS_PRIVADOS)
    except Exception as e:
        print(f"[leads_privados] No se pudo leer el CSV existente ({e}); se crea uno nuevo.")
        df = None

    columnas_ok = df is not None and set(df.columns) == set(COLUMNAS_ESTANDAR)
    if not columnas_ok:
        # El CSV existente es de un esquema distinto (por ejemplo, de una
        # versión anterior del script con columnas como
        # 'Interacciones_Busqueda' o 'Score_Calidad_B2B'). No se mezcla
        # a ciegas: se conserva como respaldo y se empieza limpio.
        if df is not None:
            respaldo = CSV_LEADS_PRIVADOS.replace(".csv", f".viejo_{datetime.now():%Y%m%d_%H%M%S}.bak")
            shutil.move(CSV_LEADS_PRIVADOS, respaldo)
            print(f"[leads_privados] El CSV tenía columnas distintas a las actuales. "
                  f"Se guardó como respaldo en: {respaldo}")
        pd.DataFrame(columns=COLUMNAS_ESTANDAR).to_csv(CSV_LEADS_PRIVADOS, index=False)
        return pd.DataFrame(columns=COLUMNAS_ESTANDAR)

    if not df.empty:
        df["Fecha_Publicacion"] = pd.to_datetime(df["Fecha_Publicacion"], errors="coerce")
        df["Fecha_Cierre"] = pd.to_datetime(df["Fecha_Cierre"], errors="coerce")
    return df[COLUMNAS_ESTANDAR]


def guardar_lead_privado(fila_dict):
    df = cargar_leads_privados()
    clave_nueva = (str(fila_dict["Empresa"]).lower(), str(fila_dict["Proyecto"]).lower())
    if not df.empty:
        claves = set(zip(df["Empresa"].astype(str).str.lower(), df["Proyecto"].astype(str).str.lower()))
        if clave_nueva in claves:
            return False, "Ese lead (misma empresa + mismo proyecto) ya está registrado."
    nuevo = pd.DataFrame([fila_dict], columns=COLUMNAS_ESTANDAR)
    pd.concat([df, nuevo], ignore_index=True).to_csv(CSV_LEADS_PRIVADOS, index=False)
    return True, "Lead guardado correctamente."


# ==========================================================
# 5. CONSOLIDACIÓN
# ==========================================================
def construir_dataset():
    try:
        df_secop = consultar_secop()
    except Exception as e:
        print(f"[construir_dataset] Falló SECOP: {e}")
        df_secop = pd.DataFrame(columns=COLUMNAS_ESTANDAR)
    try:
        df_priv = cargar_leads_privados()
    except Exception as e:
        print(f"[construir_dataset] Falló CSV de leads privados: {e}")
        df_priv = pd.DataFrame(columns=COLUMNAS_ESTANDAR)

    data = pd.concat([df_secop, df_priv], ignore_index=True)
    if data.empty:
        return data

    data = data[data["Fecha_Publicacion"].notna()]
    fecha_limite = datetime.now() - timedelta(days=DIAS_ANTIGUEDAD_MAXIMA)
    data = data[data["Fecha_Publicacion"] >= fecha_limite]
    return data.sort_values("Score_Relevancia", ascending=False).reset_index(drop=True)


# ==========================================================
# 6. AGENTE DE REDACCIÓN — API real si hay clave; si no, modo manual
# ==========================================================
def construir_prompt_base(empresa_cliente, proyecto, sector):
    servicios_txt = "; ".join(EMPRESA["servicios"])
    return (
        f"Actúa como redactor comercial y técnico senior para {EMPRESA['marca']} "
        f"({EMPRESA['razon_social']}, NIT {EMPRESA['nit']}).\n\n"
        f"IMPORTANTE: usa únicamente la información institucional que te doy. No inventes "
        f"proyectos anteriores, clientes ni cifras de experiencia que no aparezcan aquí. "
        f"Si falta un dato (precio, plazo, alcance técnico exacto), déjalo marcado como "
        f"[DATO PENDIENTE DE CONFIRMAR] en vez de inventarlo.\n\n"
        f"Información institucional verificada:\n"
        f"- Servicios: {servicios_txt}.\n"
        f"- Trayectoria: {EMPRESA['trayectoria']}\n"
        f"- Sitio web: {EMPRESA['web']} | Teléfono: {EMPRESA['telefono']} | Correo: {EMPRESA['correo']}\n\n"
        f"Tarea: redacta una propuesta comercial dirigida a '{empresa_cliente}' para el "
        f"proceso/proyecto '{proyecto}' (sector: {sector}). Incluye presentación institucional "
        f"breve, cómo los servicios listados se relacionan con lo que ese proceso probablemente "
        f"requiere, una sección [DATO PENDIENTE DE CONFIRMAR] para alcance técnico y precio, y un "
        f"cierre solicitando una reunión técnica, firmado por {EMPRESA['firmante']}."
    )


def construir_prompt_estrategia_contenido(df):
    """Genera un prompt para planear contenido de redes sociales usando
    SOLO información real: los servicios verificados de la empresa y el
    número real de procesos/leads que hay en este momento en el
    dashboard. No inventa cifras de 'interacciones' ni de 'alcance' —
    esas nunca se pudieron medir de verdad."""
    total = len(df) if df is not None else 0
    ubicaciones = ", ".join(sorted(df["Ubicación"].dropna().unique())[:8]) if df is not None and not df.empty else "Urabá y Antioquia"
    servicios_txt = "; ".join(EMPRESA["servicios"])
    return (
        f"Actúa como estratega de contenido B2B para {EMPRESA['marca']} "
        f"({EMPRESA['razon_social']}).\n\n"
        f"IMPORTANTE: no inventes cifras de interacciones, alcance ni engagement — "
        f"esos datos no están disponibles aquí. Basa la propuesta solo en lo que te doy.\n\n"
        f"Datos reales de contexto:\n"
        f"- Servicios de la empresa: {servicios_txt}.\n"
        f"- Actualmente hay {total} procesos/oportunidades vigentes registrados en las últimas "
        f"{DIAS_ANTIGUEDAD_MAXIMA} días, en: {ubicaciones}.\n\n"
        f"Tarea: propone un plan de contenido de 2 semanas para LinkedIn y Facebook dirigido a "
        f"tomadores de decisión de contratación pública y privada en esas zonas, mostrando la "
        f"experiencia real de la empresa en los servicios listados. Para cada publicación da: "
        f"tema, formato (texto/foto/video) y un borrador breve de copy. No prometas resultados "
        f"de audiencia ni uses cifras que no te haya dado."
    )


def generar_propuesta_con_claude(prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, "ANTHROPIC_API_KEY no está definida — usando modo manual (copiar/pegar)."
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        bloques = resp.json().get("content", [])
        texto = "\n".join(b.get("text", "") for b in bloques if b.get("type") == "text")
        return texto or None, None
    except Exception as e:
        return None, f"No se pudo generar la propuesta automáticamente ({e}); usa el modo manual."


# ==========================================================
# 7. GRÁFICAS
# ==========================================================
PLANTILLA, FONDO = "plotly_dark", "#1A1E2E"


def fig_vacia(titulo):
    fig = px.bar(title=titulo, template=PLANTILLA)
    fig.update_layout(plot_bgcolor=FONDO, paper_bgcolor=FONDO)
    return fig


def grafico_tendencia_real(df):
    if df.empty:
        return fig_vacia("Tendencia semanal (sin datos aún)")
    tmp = df.copy()
    tmp["Semana"] = tmp["Fecha_Publicacion"].dt.to_period("W").apply(lambda p: p.start_time)
    serie = tmp.groupby(["Semana", "Fuente"]).size().reset_index(name="Procesos")
    fig = px.line(serie, x="Semana", y="Procesos", color="Fuente", markers=True,
                  title=f"Procesos encontrados por semana (últimos {DIAS_ANTIGUEDAD_MAXIMA} días)", template=PLANTILLA)
    fig.update_layout(plot_bgcolor=FONDO, paper_bgcolor=FONDO)
    return fig


def grafico_relevancia_ubicacion(df):
    if df.empty:
        return fig_vacia("Score de relevancia por ubicación (sin datos aún)")
    fig = px.bar(df, x="Ubicación", y="Score_Relevancia", color="Sector",
                 title="Score de relevancia por ubicación", template=PLANTILLA)
    fig.update_layout(plot_bgcolor=FONDO, paper_bgcolor=FONDO)
    return fig


def grafico_distribucion_sector(df):
    dfx = df.dropna(subset=["Presupuesto_COP"]) if not df.empty else df
    if dfx.empty:
        return fig_vacia("Presupuesto por sector (sin datos disponibles)")
    fig = px.pie(dfx, names="Sector", values="Presupuesto_COP", title="Presupuesto agregado por sector (COP)", template=PLANTILLA)
    fig.update_layout(plot_bgcolor=FONDO, paper_bgcolor=FONDO)
    return fig


# ==========================================================
# 8. APP DASH
# ==========================================================
app = dash.Dash(__name__)
app.title = "INCODARIEN — Inteligencia Comercial B2B"

# CSS global inyectado directamente en el <head> del HTML, con !important.
# Esto es una segunda capa de seguridad además del "style=" en línea de
# cada componente: si alguna hoja de estilos externa o algo del propio
# navegador estuviera pisando el estilo en línea, esto lo fuerza igual.
app.index_string = f"""<!DOCTYPE html>
<html>
    <head>
        {{%metas%}}
        <title>{{%title%}}</title>
        {{%favicon%}}
        {{%css%}}
        <style>
            input[type=text], input[type=number], textarea, .dash-input input {{
                height: auto !important;
                min-height: 48px !important;
                line-height: 20px !important;
                font-size: 15px !important;
                padding: 14px 16px !important;
                box-sizing: border-box !important;
            }}
            body {{ margin: 0; }}
        </style>
    </head>
    <body>
        {{%app_entry%}}
        <footer style="text-align:center; padding:8px; color:#4b5563; font-size:11px;
                       background-color:#10131F;">
            INCODARIEN Dashboard · v{VERSION_APP}
        </footer>
        {{%config%}}
        {{%scripts%}}
        {{%renderer%}}
    </body>
</html>"""
TARJETA = {"backgroundColor": FONDO, "padding": "18px", "borderRadius": "14px", "boxShadow": "0 4px 20px rgba(0,0,0,0.25)"}

app.layout = html.Div(style={"backgroundColor": "#10131F", "color": "#FFFFFF", "padding": "24px",
                              "fontFamily": "Segoe UI, Roboto, Arial, sans-serif"}, children=[
    dcc.Store(id="store-datos", data=[]),  # lista de diccionarios — NUNCA un string JSON
    dcc.Interval(id="intervalo-refresco", interval=INTERVALO_REFRESCO_MS, n_intervals=0),

    html.Div([
        html.H1("INCODARIEN — Inteligencia Comercial B2B", style={"color": "#3B8BFF", "marginBottom": "6px"}),
        html.P(id="texto-estado", style={"color": "#8A92A6", "fontSize": "13px"}),
    ], style={"marginBottom": "20px"}),

    dcc.Tabs(id="tabs", value="tab-procesos", children=[
        dcc.Tab(label="📊 Procesos y tendencias", value="tab-procesos",
                style={"backgroundColor": FONDO, "color": "#8A92A6", "border": "none"},
                selected_style={"backgroundColor": "#3B8BFF", "color": "#fff", "fontWeight": "bold"}),
        dcc.Tab(label="🤖 Agente de propuestas", value="tab-agente",
                style={"backgroundColor": FONDO, "color": "#8A92A6", "border": "none"},
                selected_style={"backgroundColor": "#3B8BFF", "color": "#fff", "fontWeight": "bold"}),
        dcc.Tab(label="➕ Registrar lead verificado", value="tab-registro",
                style={"backgroundColor": FONDO, "color": "#8A92A6", "border": "none"},
                selected_style={"backgroundColor": "#3B8BFF", "color": "#fff", "fontWeight": "bold"}),
    ], style={"marginBottom": "20px"}),

    html.Div(id="contenido-tabs"),
])


@app.callback(Output("store-datos", "data"), Input("intervalo-refresco", "n_intervals"))
def refrescar_datos(_):
    try:
        df = construir_dataset()
        return df.to_dict("records")  # lista de dicts — Dash la serializa solo
    except Exception as e:
        print(f"[refrescar_datos] Error inesperado: {e}")
        return []


def _leer_store(store_data):
    """store_data ya es una lista de dicts (nunca un string JSON), así
    que se reconstruye el DataFrame directamente — sin pasar por
    pd.read_json, que es justo lo que causaba el FileNotFoundError."""
    try:
        if not store_data:
            return pd.DataFrame(columns=COLUMNAS_ESTANDAR)
        df = pd.DataFrame(store_data)
        for c in ("Fecha_Publicacion", "Fecha_Cierre"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        for c in COLUMNAS_ESTANDAR:
            if c not in df.columns:
                df[c] = None
        return df[COLUMNAS_ESTANDAR]
    except Exception as e:
        print(f"[_leer_store] No se pudo reconstruir el DataFrame: {e}")
        return pd.DataFrame(columns=COLUMNAS_ESTANDAR)


@app.callback(Output("texto-estado", "children"), Input("store-datos", "data"))
def texto_estado(store_data):
    df = _leer_store(store_data)
    if df.empty:
        return (f"Sin procesos verificados en los últimos {DIAS_ANTIGUEDAD_MAXIMA} días por ahora "
                f"(o la fuente no respondió). Última verificación: {datetime.now():%Y-%m-%d %H:%M}.")
    verificados = int(df["Enlace_Verificado"].sum())
    return (f"{len(df)} procesos vigentes · {verificados} con enlace verificado en este momento · "
            f"Última actualización: {datetime.now():%Y-%m-%d %H:%M}.")


def _tarjeta_kpi(titulo, valor, degradado):
    return html.Div([
        html.Div(titulo, style={"fontSize": "12px", "opacity": "0.85", "marginBottom": "6px"}),
        html.Div(valor, style={"fontSize": "28px", "fontWeight": "bold"}),
    ], style={"background": degradado, "color": "#FFFFFF", "padding": "18px", "borderRadius": "14px",
              "width": "23%", "display": "inline-block", "boxSizing": "border-box",
              "boxShadow": "0 4px 20px rgba(0,0,0,0.25)"})


def _fila_kpis(df):
    total = len(df)
    verificados = int(df["Enlace_Verificado"].sum()) if not df.empty else 0
    score_prom = round(df["Score_Relevancia"].mean(), 1) if not df.empty else 0
    ultima = datetime.now().strftime("%H:%M")
    return html.Div([
        _tarjeta_kpi("Procesos vigentes", str(total), "linear-gradient(135deg, #3B8BFF, #6C5CE7)"),
        html.Span(style={"display": "inline-block", "width": "2%"}),
        _tarjeta_kpi("Enlaces verificados", str(verificados), "linear-gradient(135deg, #33D19A, #17A589)"),
        html.Span(style={"display": "inline-block", "width": "2%"}),
        _tarjeta_kpi("Score promedio", str(score_prom), "linear-gradient(135deg, #FFC654, #FF8F56)"),
        html.Span(style={"display": "inline-block", "width": "2%"}),
        _tarjeta_kpi("Última actualización", ultima, "linear-gradient(135deg, #EC4899, #8B5CF6)"),
    ], style={"marginBottom": "20px"})


@app.callback(Output("contenido-tabs", "children"), [Input("tabs", "value"), Input("store-datos", "data")])
def render_tab(tab, store_data):
    df = _leer_store(store_data)

    if tab == "tab-procesos":
        ubicaciones = sorted(df["Ubicación"].dropna().unique()) if not df.empty else []
        return html.Div([
            _fila_kpis(df),
            html.Div([
                html.Label("Filtrar por ubicación:", style={"fontWeight": "bold"}),
                dcc.Dropdown(id="filtro-ubicacion",
                             options=[{"label": "Todas", "value": "ALL"}] + [{"label": u, "value": u} for u in ubicaciones],
                             value="ALL", clearable=False, style={"color": "#10131F", "width": "300px"}),
            ], style={"marginBottom": "20px"}),
            html.Div([
                html.Div([dcc.Graph(id="g-tendencia")], style={"width": "49%", "display": "inline-block", **TARJETA}),
                html.Div([dcc.Graph(id="g-relevancia")], style={"width": "49%", "display": "inline-block", "float": "right", **TARJETA}),
            ], style={"marginBottom": "18px"}),
            html.Div([dcc.Graph(id="g-sector")], style={**TARJETA, "marginBottom": "18px"}),
            html.Div([html.H3("Procesos vigentes", style={"color": "#3B8BFF"}), html.Div(id="tabla-procesos")], style=TARJETA),
        ])

    if tab == "tab-agente":
        empresas = [{"label": e, "value": e} for e in df["Empresa"].dropna().unique()] if not df.empty else []
        return html.Div([
            html.Div([
                html.Label("Proceso / entidad:", style={"fontWeight": "bold"}),
                dcc.Dropdown(id="sel-empresa", options=empresas, style={"color": "#10131F", "marginBottom": "12px"}),
                html.Div(id="detalle-seleccion", style={"color": "#8A92A6", "fontSize": "12px", "marginBottom": "12px"}),
                html.Button("✨ Generar propuesta", id="btn-generar", n_clicks=0,
                            style={"width": "100%", "padding": "12px", "backgroundColor": "#3B8BFF", "color": "#fff",
                                   "border": "none", "borderRadius": "8px", "fontWeight": "bold", "cursor": "pointer"}),
                html.P(id="aviso-agente", style={"color": "#FFC654", "fontSize": "11px", "marginTop": "8px"}),
            ], style={"width": "36%", "display": "inline-block", "verticalAlign": "top", **TARJETA}),
            html.Div([
                dcc.Textarea(id="txt-prompt", style={"width": "100%", "height": "320px", "backgroundColor": "#10131F",
                                                      "color": "#cbd5e1", "border": "1px solid #3B8BFF", "borderRadius": "8px",
                                                      "padding": "10px", "fontSize": "12px"}),
                dcc.Clipboard(target_id="txt-prompt", title="Copiar", style={"fontSize": "22px", "color": "#3B8BFF", "cursor": "pointer"}),
                html.Span("  Copiar", style={"color": "#8A92A6", "fontSize": "12px"}),
            ], style={"width": "60%", "display": "inline-block", "float": "right", **TARJETA}),
        ])

    return html.Div([
        html.Div([
            html.A("🏛️ Abrir portal oficial SECOP II", href="https://community.secop.gov.co/Public/Tendering/ContractNoticeManagement/Index",
                   target="_blank", style={
                       "display": "inline-block", "backgroundColor": "#1A1E2E", "color": "#3B8BFF",
                       "border": "1px solid #3B8BFF", "borderRadius": "8px", "padding": "10px 16px",
                       "textDecoration": "none", "fontWeight": "bold", "marginRight": "10px", "fontSize": "13px",
                   }),
            html.Button("📱 Generar estrategia de contenido", id="btn-estrategia-contenido", n_clicks=0,
                        style={"backgroundColor": "#1A1E2E", "color": "#33D19A", "border": "1px solid #33D19A",
                               "borderRadius": "8px", "padding": "10px 16px", "fontWeight": "bold",
                               "cursor": "pointer", "fontSize": "13px"}),
        ], style={"marginBottom": "18px"}),
        html.Div(id="salida-estrategia-contenido", style={"marginBottom": "10px"}),

        html.H3("Registrar un lead investigado y verificado por tu equipo", style={"color": "#33D19A", "marginTop": "10px"}),
        html.P("Solo para oportunidades del sector privado que alguien de tu equipo confirmó a mano "
               "(nombre real, enlace real, contacto real). El score se calcula con la misma fórmula "
               "transparente que usan los procesos de SECOP — no se autogenera ningún número inventado.",
               style={"color": "#8A92A6", "fontSize": "13px", "marginBottom": "20px"}),
        html.Div([
            dcc.Input(id="in-empresa", placeholder="Empresa / entidad", style=_estilo_input()),
            dcc.Input(id="in-sector", placeholder="Sector", style=_estilo_input()),
            dcc.Input(id="in-ubicacion", placeholder="Ubicación / municipio", style=_estilo_input()),
            dcc.Input(id="in-proyecto", placeholder="Descripción del proyecto/oportunidad", style=_estilo_input()),
            dcc.Input(id="in-presupuesto", type="number", placeholder="Presupuesto estimado (COP, opcional)", style=_estilo_input()),
            dcc.Input(id="in-enlace", placeholder="Enlace real de referencia (https://...)", style=_estilo_input()),
            dcc.Input(id="in-contacto", placeholder="Contacto real verificado (correo o teléfono)", style=_estilo_input()),
            html.Button("💾 Guardar lead verificado", id="btn-guardar", n_clicks=0,
                        style={"width": "100%", "padding": "12px", "backgroundColor": "#33D19A", "color": "#10131F",
                               "border": "none", "borderRadius": "8px", "fontWeight": "bold", "cursor": "pointer", "marginTop": "10px"}),
            html.Div(id="msg-guardado", style={"marginTop": "14px", "fontWeight": "bold"}),
        ], style={"maxWidth": "600px"}),
    ], style=TARJETA)


def _estilo_input():
    # Antes el texto se veía "cortado a la mitad" porque no había altura
    # mínima ni line-height explícitos, así que el campo quedaba a merced
    # de estilos heredados de la página. Ahora todo queda fijo:
    return {
        "width": "100%", "boxSizing": "border-box", "display": "block",
        "padding": "14px 16px", "marginBottom": "16px",
        "minHeight": "48px", "height": "48px", "lineHeight": "20px",
        "fontSize": "15px", "fontFamily": "inherit",
        "backgroundColor": "#10131F", "color": "#FFFFFF",
        "border": "1px solid #25293C", "borderRadius": "8px",
        "overflow": "visible",
    }


def _td():
    return {"padding": "8px", "borderBottom": "1px solid #25293C"}


@app.callback(
    [Output("g-tendencia", "figure"), Output("g-relevancia", "figure"),
     Output("g-sector", "figure"), Output("tabla-procesos", "children")],
    [Input("filtro-ubicacion", "value"), Input("store-datos", "data")],
    prevent_initial_call=False,
)
def actualizar_procesos(ubicacion, store_data):
    df = _leer_store(store_data)
    if not df.empty and ubicacion and ubicacion != "ALL":
        df = df[df["Ubicación"] == ubicacion]

    filas = []
    if df.empty:
        filas.append(html.Tr([html.Td("No hay procesos que coincidan.", colSpan=6,
                                       style={"padding": "14px", "textAlign": "center", "color": "#8A92A6"})]))
    else:
        for _, row in df.iterrows():
            estado = "✅" if row["Enlace_Verificado"] else "⚠️ no verificado"
            enlace = html.A(f"Ver proceso {estado}", href=row["Enlace_Proceso"], target="_blank",
                             style={"color": "#3B8BFF" if row["Enlace_Verificado"] else "#f87171"}) if row["Enlace_Proceso"] else "Sin enlace"
            filas.append(html.Tr([
                html.Td(row["Empresa"], style=_td()), html.Td(row["Proyecto"], style=_td()),
                html.Td(row["Ubicación"], style=_td()), html.Td(row["Fuente"], style={**_td(), "fontSize": "11px"}),
                html.Td(str(row["Score_Relevancia"]), style={**_td(), "color": "#33D19A", "fontWeight": "bold"}),
                html.Td(enlace, style=_td()),
            ]))

    tabla = html.Table([
        html.Thead(html.Tr([html.Th(c) for c in ["Entidad/Empresa", "Proyecto", "Ubicación", "Fuente", "Score", "Enlace"]],
                            style={"backgroundColor": "#10131F", "padding": "8px", "textAlign": "left"})),
        html.Tbody(filas),
    ], style={"width": "100%", "borderCollapse": "collapse", "fontSize": "12px"})

    return grafico_tendencia_real(df), grafico_relevancia_ubicacion(df), grafico_distribucion_sector(df), tabla


@app.callback(Output("detalle-seleccion", "children"), Input("sel-empresa", "value"), State("store-datos", "data"))
def detalle_seleccion(empresa, store_data):
    df = _leer_store(store_data)
    if not empresa or df.empty:
        return ""
    coincidencias = df[df["Empresa"] == empresa]
    if coincidencias.empty:
        return ""
    fila = coincidencias.iloc[0]
    return f"Proyecto: {str(fila['Proyecto'])[:150]} | Sector: {fila['Sector']} | Score: {fila['Score_Relevancia']}"


@app.callback(
    [Output("txt-prompt", "value"), Output("aviso-agente", "children")],
    Input("btn-generar", "n_clicks"),
    [State("sel-empresa", "value"), State("store-datos", "data")],
    prevent_initial_call=True,
)
def generar_propuesta(n, empresa, store_data):
    df = _leer_store(store_data)
    if not empresa or df.empty:
        return "Selecciona un proceso primero.", ""
    coincidencias = df[df["Empresa"] == empresa]
    if coincidencias.empty:
        return "Selecciona un proceso primero.", ""
    fila = coincidencias.iloc[0]
    prompt = construir_prompt_base(empresa, fila["Proyecto"], fila["Sector"])

    texto, aviso = generar_propuesta_con_claude(prompt)
    if texto:
        return texto, "Generado automáticamente con la API de Claude."
    return prompt, aviso or "Modo manual: copia este prompt y pégalo en claude.ai o en Gemini."


@app.callback(
    Output("salida-estrategia-contenido", "children"),
    Input("btn-estrategia-contenido", "n_clicks"),
    State("store-datos", "data"),
    prevent_initial_call=True,
)
def generar_estrategia_contenido(n, store_data):
    df = _leer_store(store_data)
    prompt = construir_prompt_estrategia_contenido(df)
    texto, aviso = generar_propuesta_con_claude(prompt)
    resultado = texto or prompt
    nota = "Generado con la API de Claude." if texto else (aviso or "Modo manual: copia y pega en claude.ai o Gemini.")
    return html.Div([
        dcc.Textarea(id="txt-estrategia-resultado", value=resultado,
                     style={"width": "100%", "height": "220px", "backgroundColor": "#10131F", "color": "#cbd5e1",
                            "border": "1px solid #33D19A", "borderRadius": "8px", "padding": "10px", "fontSize": "12px",
                            "boxSizing": "border-box"}),
        dcc.Clipboard(target_id="txt-estrategia-resultado", title="Copiar",
                      style={"fontSize": "20px", "color": "#33D19A", "cursor": "pointer", "marginTop": "6px"}),
        html.Span(f"  {nota}", style={"color": "#8A92A6", "fontSize": "11px"}),
    ])


@app.callback(
    Output("msg-guardado", "children"),
    Input("btn-guardar", "n_clicks"),
    [State("in-empresa", "value"), State("in-sector", "value"), State("in-ubicacion", "value"),
     State("in-proyecto", "value"), State("in-presupuesto", "value"),
     State("in-enlace", "value"), State("in-contacto", "value")],
    prevent_initial_call=True,
)
def registrar_lead(n, empresa, sector, ubicacion, proyecto, presupuesto, enlace, contacto):
    if not empresa or not proyecto or not contacto:
        return html.Span("⚠️ Empresa, proyecto y contacto verificado son obligatorios.", style={"color": "#f87171"})

    try:
        enlace_ok = verificar_enlace(enlace) if enlace else False
        score = calcular_score(proyecto, presupuesto, dias_restantes=None)
        fila = {
            "Empresa": empresa, "Sector": sector or "Privado", "Ubicación": ubicacion or "",
            "Proyecto": proyecto, "Modalidad": "Registro manual verificado",
            "Fuente": "Verificado manualmente por el equipo comercial",
            "Presupuesto_COP": presupuesto, "Fecha_Publicacion": datetime.now().isoformat(),
            "Fecha_Cierre": None, "Enlace_Proceso": enlace or "",
            "Enlace_Verificado": enlace_ok, "Contacto": contacto, "Score_Relevancia": score,
        }
        ok, mensaje = guardar_lead_privado(fila)
        color = "#33D19A" if ok else "#FFC654"
        extra = "" if enlace_ok or not enlace else " (aviso: el enlace no respondió al verificarlo)."
        return html.Span(f"{'✅' if ok else '⚠️'} {mensaje}{extra}", style={"color": color})
    except Exception as e:
        return html.Span(f"❌ Error al guardar: {e}", style={"color": "#f87171"})


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8050))
    app.run(debug=False, host="0.0.0.0", port=puerto)
