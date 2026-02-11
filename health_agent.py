import os
import sys
import json
from datetime import datetime, timedelta
import pytz
import requests

print("=" * 60, flush=True)
print("A30.ai - Health Agent (Diagnóstico + Auto-Repair)", flush=True)
print("=" * 60, flush=True)

# === CONFIGURACIÓN ===
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")

GCP_PROJECT = "creedenciales-lovable"
GCP_REGION = "europe-southwest1"
JOBS_TO_MONITOR = {
    "a30ai-video-processor": {"expected_hour": 21, "description": "Procesador de vídeos"},
    "a30ai-daily-report": {"expected_hour": 23, "description": "Informe diario por email"},
}

DESTINATARIO = "ciglesias.agra@gmail.com"
REMITENTE = "sesiones@a30.fm"

MADRID_TZ = pytz.timezone("Europe/Madrid")
AHORA = datetime.now(MADRID_TZ)
AYER = AHORA - timedelta(days=1)

# === ESTADO GLOBAL ===
problemas = []
reparaciones = []
estado_servicios = {}


def check_env_vars():
    """Verifica que todas las variables de entorno estén configuradas"""
    print("\n📋 Verificando variables de entorno...", flush=True)
    vars_needed = {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "RESEND_API_KEY": RESEND_API_KEY,
        "GOOGLE_CREDENTIALS": GOOGLE_CREDENTIALS,
    }
    for name, val in vars_needed.items():
        if not val:
            problemas.append(f"⚠️ Variable de entorno {name} no configurada")
            print(f"  ❌ {name}: FALTA", flush=True)
        else:
            print(f"  ✅ {name}: OK", flush=True)


def get_gcp_token():
    """Obtiene token de autenticación de GCP usando la service account"""
    import google.auth
    from google.auth.transport.requests import Request
    import google.oauth2.service_account

    try:
        if GOOGLE_CREDENTIALS:
            creds_info = json.loads(GOOGLE_CREDENTIALS)
            creds = google.oauth2.service_account.Credentials.from_service_account_info(
                creds_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        problemas.append(f"❌ No se pudo autenticar con GCP: {e}")
        print(f"  ❌ GCP Auth: {e}", flush=True)
        return None


def check_cloud_run_jobs():
    """Verifica el estado de los Cloud Run Jobs de anoche"""
    print("\n🔍 Verificando Cloud Run Jobs...", flush=True)

    token = get_gcp_token()
    if not token:
        return

    headers = {"Authorization": f"Bearer {token}"}

    for job_name, config in JOBS_TO_MONITOR.items():
        print(f"\n  Verificando {job_name}...", flush=True)

        # Listar ejecuciones del job
        url = (
            f"https://run.googleapis.com/v2/projects/{GCP_PROJECT}/"
            f"locations/{GCP_REGION}/jobs/{job_name}/executions"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                problemas.append(
                    f"❌ No se pudo consultar {job_name}: HTTP {resp.status_code}"
                )
                print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                estado_servicios[job_name] = "❌ No accesible"
                continue

            data = resp.json()
            executions = data.get("executions", [])

            if not executions:
                problemas.append(f"⚠️ {job_name}: No hay ejecuciones registradas")
                estado_servicios[job_name] = "⚠️ Sin ejecuciones"
                continue

            # La primera ejecución es la más reciente
            latest = executions[0]
            completion_time = latest.get("completionTime") or latest.get("createTime", "")
            conditions = latest.get("conditions", [])

            # Determinar estado
            succeeded = False
            failed = False
            for cond in conditions:
                if cond.get("type") == "Completed":
                    if cond.get("state") == "CONDITION_SUCCEEDED":
                        succeeded = True
                    elif cond.get("state") == "CONDITION_FAILED":
                        failed = True

            # Verificar si se ejecutó ayer/anoche
            if completion_time:
                exec_dt = datetime.fromisoformat(
                    completion_time.replace("Z", "+00:00")
                ).astimezone(MADRID_TZ)
                hours_ago = (AHORA - exec_dt).total_seconds() / 3600
                exec_str = exec_dt.strftime("%d/%m/%Y %H:%M")
            else:
                hours_ago = 999
                exec_str = "desconocido"

            if failed:
                estado_servicios[job_name] = f"❌ Falló ({exec_str})"
                problemas.append(
                    f"❌ {config['description']} ({job_name}) falló. "
                    f"Última ejecución: {exec_str}"
                )
                # AUTO-REPAIR: Re-ejecutar el job
                print(f"  ❌ FALLÓ - Intentando re-ejecutar...", flush=True)
                repair_cloud_run_job(job_name, config["description"], token)

            elif succeeded and hours_ago < 24:
                estado_servicios[job_name] = f"✅ OK ({exec_str})"
                print(f"  ✅ Ejecutado correctamente: {exec_str}", flush=True)

            elif hours_ago >= 24:
                estado_servicios[job_name] = f"⚠️ No se ejecutó anoche ({exec_str})"
                problemas.append(
                    f"⚠️ {config['description']} ({job_name}) no se ejecutó anoche. "
                    f"Última ejecución: {exec_str}"
                )
                # AUTO-REPAIR: Re-ejecutar
                print(
                    f"  ⚠️ No se ejecutó anoche - Intentando re-ejecutar...", flush=True
                )
                repair_cloud_run_job(job_name, config["description"], token)
            else:
                estado_servicios[job_name] = f"🔄 En curso ({exec_str})"
                print(f"  🔄 En ejecución: {exec_str}", flush=True)

        except Exception as e:
            problemas.append(f"❌ Error consultando {job_name}: {e}")
            estado_servicios[job_name] = f"❌ Error: {e}"
            print(f"  ❌ Error: {e}", flush=True)


def repair_cloud_run_job(job_name, description, token):
    """Re-ejecuta un Cloud Run Job que falló"""
    url = (
        f"https://run.googleapis.com/v2/projects/{GCP_PROJECT}/"
        f"locations/{GCP_REGION}/jobs/{job_name}:run"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, headers=headers, json={}, timeout=30)
        if resp.status_code in (200, 202):
            reparaciones.append(
                f"🔧 Re-ejecutado {description} ({job_name}) automáticamente"
            )
            print(f"  🔧 Re-ejecutado correctamente", flush=True)
        else:
            problemas.append(
                f"❌ No se pudo re-ejecutar {job_name}: HTTP {resp.status_code}"
            )
            print(
                f"  ❌ No se pudo re-ejecutar: HTTP {resp.status_code} - {resp.text[:200]}",
                flush=True,
            )
    except Exception as e:
        problemas.append(f"❌ Error re-ejecutando {job_name}: {e}")
        print(f"  ❌ Error re-ejecutando: {e}", flush=True)


def check_supabase():
    """Verifica que Supabase responde y hay datos recientes"""
    print("\n🗄️ Verificando Supabase...", flush=True)

    if not SUPABASE_URL or not SUPABASE_KEY:
        problemas.append("❌ Supabase: credenciales no configuradas")
        estado_servicios["Supabase"] = "❌ Sin credenciales"
        return

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

    # Test 1: ¿Responde Supabase?
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/sesiones?select=count&limit=1",
            headers={**headers, "Prefer": "count=exact"},
            timeout=15,
        )
        if resp.status_code != 200:
            problemas.append(f"❌ Supabase no responde: HTTP {resp.status_code}")
            estado_servicios["Supabase"] = f"❌ HTTP {resp.status_code}"
            return

        # Obtener total de sesiones desde el header Content-Range
        content_range = resp.headers.get("Content-Range", "")
        total = content_range.split("/")[-1] if "/" in content_range else "?"
        print(f"  ✅ Supabase responde. Total sesiones: {total}", flush=True)

    except Exception as e:
        problemas.append(f"❌ Supabase no accesible: {e}")
        estado_servicios["Supabase"] = f"❌ {e}"
        return

    # Test 2: ¿Hay sesiones de ayer?
    ayer_str = AYER.strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/sesiones"
            f"?select=id&fecha_sesion=gte.{ayer_str}T00:00:00"
            f"&fecha_sesion=lte.{ayer_str}T23:59:59",
            headers={**headers, "Prefer": "count=exact"},
            timeout=15,
        )

        content_range = resp.headers.get("Content-Range", "")
        count = content_range.split("/")[-1] if "/" in content_range else "0"

        if count == "0" or count == "?":
            # Intentar con created_at por si fecha_sesion está vacío
            resp2 = requests.get(
                f"{SUPABASE_URL}/rest/v1/sesiones"
                f"?select=id&created_at=gte.{ayer_str}T00:00:00"
                f"&created_at=lte.{ayer_str}T23:59:59",
                headers={**headers, "Prefer": "count=exact"},
                timeout=15,
            )
            content_range2 = resp2.headers.get("Content-Range", "")
            count2 = content_range2.split("/")[-1] if "/" in content_range2 else "0"

            if count2 == "0" or count2 == "?":
                problemas.append(
                    f"⚠️ No hay sesiones nuevas de ayer ({ayer_str}) en Supabase"
                )
                estado_servicios["Supabase"] = f"⚠️ 0 sesiones ayer ({total} total)"
            else:
                estado_servicios["Supabase"] = (
                    f"✅ {count2} sesiones ayer (created_at) ({total} total)"
                )
                print(
                    f"  ✅ Sesiones de ayer (created_at): {count2}", flush=True
                )
        else:
            estado_servicios["Supabase"] = (
                f"✅ {count} sesiones ayer ({total} total)"
            )
            print(f"  ✅ Sesiones de ayer: {count}", flush=True)

    except Exception as e:
        problemas.append(f"⚠️ Error consultando sesiones de ayer: {e}")
        estado_servicios["Supabase"] = f"⚠️ Error consulta: {e}"


def check_openai():
    """Verifica la API de OpenAI y el saldo restante"""
    print("\n🤖 Verificando OpenAI...", flush=True)

    if not OPENAI_API_KEY:
        problemas.append("❌ OpenAI: API key no configurada")
        estado_servicios["OpenAI"] = "❌ Sin API key"
        return

    # Test 1: ¿La API key es válida?
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    try:
        resp = requests.get(
            "https://api.openai.com/v1/models",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 401:
            problemas.append("❌ OpenAI: API key inválida o expirada")
            estado_servicios["OpenAI"] = "❌ API key inválida"
            return
        elif resp.status_code == 429:
            problemas.append("❌ OpenAI: Rate limit / Cuota agotada (429)")
            estado_servicios["OpenAI"] = "❌ Cuota agotada (429)"
            return
        elif resp.status_code != 200:
            problemas.append(f"⚠️ OpenAI responde con HTTP {resp.status_code}")
            estado_servicios["OpenAI"] = f"⚠️ HTTP {resp.status_code}"
            return

        print(f"  ✅ API key válida", flush=True)

    except Exception as e:
        problemas.append(f"❌ OpenAI no accesible: {e}")
        estado_servicios["OpenAI"] = f"❌ {e}"
        return

    # Test 2: Verificar saldo/billing
    try:
        # Consultar uso del mes actual
        now = datetime.utcnow()
        start_date = now.strftime("%Y-%m-01")
        end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        resp = requests.get(
            f"https://api.openai.com/v1/organization/usage/completions"
            f"?start_time={int(datetime(now.year, now.month, 1).timestamp())}"
            f"&bucket_width=1d",
            headers=headers,
            timeout=15,
        )

        # Intentar obtener info de billing/subscription
        resp_billing = requests.get(
            "https://api.openai.com/v1/organization/billing/subscription",
            headers=headers,
            timeout=15,
        )

        if resp_billing.status_code == 200:
            billing = resp_billing.json()
            has_payment = billing.get("has_payment_method", False)
            plan = billing.get("plan", {}).get("title", "desconocido")
            estado_servicios["OpenAI"] = f"✅ Plan: {plan}, Pago: {'Sí' if has_payment else 'No'}"
            print(f"  ✅ Plan: {plan}, Método de pago: {'Sí' if has_payment else 'No'}", flush=True)

            if not has_payment:
                problemas.append("⚠️ OpenAI: No hay método de pago configurado")
        else:
            # Si no podemos acceder a billing, al menos la API funciona
            estado_servicios["OpenAI"] = "✅ API funciona (billing no accesible)"
            print(f"  ✅ API funciona (billing no accesible)", flush=True)

    except Exception as e:
        estado_servicios["OpenAI"] = f"✅ API funciona (billing check falló: {e})"
        print(f"  ✅ API funciona, billing check falló: {e}", flush=True)


def check_anthropic():
    """Verifica la API de Anthropic"""
    print("\n🧠 Verificando Anthropic...", flush=True)

    if not ANTHROPIC_API_KEY:
        problemas.append("❌ Anthropic: API key no configurada")
        estado_servicios["Anthropic"] = "❌ Sin API key"
        return

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    try:
        # Hacer una llamada mínima para verificar que la API funciona
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=30,
        )

        if resp.status_code == 200:
            estado_servicios["Anthropic"] = "✅ API funciona"
            print(f"  ✅ API funciona correctamente", flush=True)
        elif resp.status_code == 401:
            problemas.append("❌ Anthropic: API key inválida")
            estado_servicios["Anthropic"] = "❌ API key inválida"
        elif resp.status_code == 429:
            problemas.append("❌ Anthropic: Rate limit / Sin crédito")
            estado_servicios["Anthropic"] = "❌ Rate limit / Sin crédito"
        elif resp.status_code == 400:
            # 400 con API key válida puede ser un error de request, pero la key funciona
            estado_servicios["Anthropic"] = "✅ API key válida"
            print(f"  ✅ API key válida", flush=True)
        else:
            problemas.append(f"⚠️ Anthropic: HTTP {resp.status_code}")
            estado_servicios["Anthropic"] = f"⚠️ HTTP {resp.status_code}"

    except Exception as e:
        problemas.append(f"❌ Anthropic no accesible: {e}")
        estado_servicios["Anthropic"] = f"❌ {e}"


def check_resend():
    """Verifica la API de Resend"""
    print("\n📧 Verificando Resend...", flush=True)

    if not RESEND_API_KEY:
        problemas.append("❌ Resend: API key no configurada")
        estado_servicios["Resend"] = "❌ Sin API key"
        return

    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}

    try:
        resp = requests.get(
            "https://api.resend.com/domains",
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 200:
            domains = resp.json().get("data", [])
            domain_names = [d.get("name", "?") for d in domains]
            estado_servicios["Resend"] = f"✅ Dominios: {', '.join(domain_names)}"
            print(f"  ✅ Dominios verificados: {', '.join(domain_names)}", flush=True)
        elif resp.status_code == 401:
            problemas.append("❌ Resend: API key inválida")
            estado_servicios["Resend"] = "❌ API key inválida"
        else:
            problemas.append(f"⚠️ Resend: HTTP {resp.status_code}")
            estado_servicios["Resend"] = f"⚠️ HTTP {resp.status_code}"

    except Exception as e:
        problemas.append(f"❌ Resend no accesible: {e}")
        estado_servicios["Resend"] = f"❌ {e}"


def check_cloud_schedulers():
    """Verifica que los Cloud Schedulers existen y están activos"""
    print("\n⏰ Verificando Cloud Schedulers...", flush=True)

    token = get_gcp_token()
    if not token:
        return

    headers = {"Authorization": f"Bearer {token}"}

    # Listar todos los schedulers
    # Los schedulers pueden estar en europe-west1 o europe-southwest1
    for region in ["europe-west1", "europe-southwest1"]:
        url = (
            f"https://cloudscheduler.googleapis.com/v1/"
            f"projects/{GCP_PROJECT}/locations/{region}/jobs"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                for job in jobs:
                    name = job.get("name", "").split("/")[-1]
                    state = job.get("state", "UNKNOWN")
                    schedule = job.get("schedule", "?")
                    last_attempt = job.get("lastAttemptTime", "nunca")
                    status = job.get("status", {})
                    last_code = status.get("code", None)

                    if state == "ENABLED":
                        state_icon = "✅"
                    elif state == "PAUSED":
                        state_icon = "⏸️"
                        problemas.append(f"⚠️ Scheduler {name} está pausado")
                    else:
                        state_icon = "❓"

                    if last_code and last_code != 0:
                        problemas.append(
                            f"⚠️ Scheduler {name}: última ejecución con error (code={last_code})"
                        )

                    estado_servicios[f"Scheduler: {name}"] = (
                        f"{state_icon} {schedule} ({region})"
                    )
                    print(
                        f"  {state_icon} {name}: {schedule} ({region})", flush=True
                    )

        except Exception as e:
            print(f"  ⚠️ Error consultando schedulers en {region}: {e}", flush=True)

    # Verificar que existe un scheduler para daily-report
    scheduler_names = [
        k.replace("Scheduler: ", "")
        for k in estado_servicios
        if k.startswith("Scheduler:")
    ]
    has_daily_report_scheduler = any(
        "daily-report" in name or "daily_report" in name for name in scheduler_names
    )
    if not has_daily_report_scheduler:
        problemas.append(
            "❌ No existe Cloud Scheduler para a30ai-daily-report. "
            "El informe diario NO se ejecuta automáticamente."
        )


def generar_html_alerta():
    """Genera el email HTML con el diagnóstico"""

    hay_problemas = len(problemas) > 0
    hay_reparaciones = len(reparaciones) > 0

    if hay_problemas:
        titulo = "⚠️ Health Check A30.ai - Problemas detectados"
        color_header = "#dc3545"
    else:
        titulo = "✅ Health Check A30.ai - Todo OK"
        color_header = "#28a745"

    # Tabla de servicios
    filas_servicios = ""
    for servicio, estado in estado_servicios.items():
        if "❌" in estado:
            bg = "#fff5f5"
        elif "⚠️" in estado:
            bg = "#fffbeb"
        else:
            bg = "#f0fdf4"

        filas_servicios += f"""
        <tr style="background-color: {bg};">
            <td style="padding: 10px; border-bottom: 1px solid #eee; font-weight: bold;">{servicio}</td>
            <td style="padding: 10px; border-bottom: 1px solid #eee;">{estado}</td>
        </tr>"""

    # Lista de problemas
    problemas_html = ""
    if hay_problemas:
        items = "".join(f"<li style='margin-bottom: 8px;'>{p}</li>" for p in problemas)
        problemas_html = f"""
        <div style="background-color: #fff5f5; border-left: 4px solid #dc3545; padding: 15px; margin: 20px 0; border-radius: 4px;">
            <h3 style="color: #dc3545; margin-top: 0;">Problemas detectados ({len(problemas)})</h3>
            <ul style="margin: 0; padding-left: 20px;">{items}</ul>
        </div>"""

    # Lista de reparaciones
    reparaciones_html = ""
    if hay_reparaciones:
        items = "".join(
            f"<li style='margin-bottom: 8px;'>{r}</li>" for r in reparaciones
        )
        reparaciones_html = f"""
        <div style="background-color: #f0f9ff; border-left: 4px solid #3b82f6; padding: 15px; margin: 20px 0; border-radius: 4px;">
            <h3 style="color: #3b82f6; margin-top: 0;">Reparaciones automáticas ({len(reparaciones)})</h3>
            <ul style="margin: 0; padding-left: 20px;">{items}</ul>
        </div>"""

    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #333;">
        <div style="background-color: {color_header}; color: white; padding: 20px; border-radius: 8px 8px 0 0; text-align: center;">
            <h1 style="margin: 0; font-size: 22px;">{titulo}</h1>
            <p style="margin: 5px 0 0; opacity: 0.9;">{AHORA.strftime('%d/%m/%Y %H:%M')} (hora Madrid)</p>
        </div>

        <div style="background-color: #fff; border: 1px solid #e5e7eb; border-top: none; padding: 20px; border-radius: 0 0 8px 8px;">

            <h2 style="color: #374151; border-bottom: 2px solid #e5e7eb; padding-bottom: 10px;">
                Estado de servicios
            </h2>
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="background-color: #f9fafb;">
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #e5e7eb;">Servicio</th>
                        <th style="padding: 10px; text-align: left; border-bottom: 2px solid #e5e7eb;">Estado</th>
                    </tr>
                </thead>
                <tbody>
                    {filas_servicios}
                </tbody>
            </table>

            {problemas_html}
            {reparaciones_html}

            <div style="margin-top: 20px; padding: 15px; background-color: #f9fafb; border-radius: 4px; font-size: 13px; color: #6b7280;">
                <strong>Health Agent A30.ai</strong> — Diagnóstico automático ejecutado a las {AHORA.strftime('%H:%M')}.
                {f"Se intentaron {len(reparaciones)} reparaciones automáticas." if hay_reparaciones else ""}
                {"No se requiere acción." if not hay_problemas else "Revisa los problemas indicados arriba."}
            </div>
        </div>
    </body>
    </html>
    """

    return html


def enviar_alerta(html):
    """Envía el email de diagnóstico"""
    import resend as resend_lib

    resend_lib.api_key = RESEND_API_KEY

    hay_problemas = len(problemas) > 0

    if hay_problemas:
        asunto = f"⚠️ A30.ai Health Check - {len(problemas)} problema(s) detectado(s)"
    else:
        asunto = f"✅ A30.ai Health Check - Todo OK ({AHORA.strftime('%d/%m')})"

    try:
        params = {
            "from": f"A30.ai Health Agent <{REMITENTE}>",
            "to": [DESTINATARIO],
            "subject": asunto,
            "html": html,
        }

        email = resend_lib.Emails.send(params)
        print(f"\n✅ Email de diagnóstico enviado: {email}", flush=True)
        return True

    except Exception as e:
        print(f"\n❌ Error enviando email de diagnóstico: {e}", flush=True)
        return False


def main():
    try:
        # 1. Variables de entorno
        check_env_vars()

        # 2. Estado de Cloud Run Jobs (+ auto-repair si fallan)
        check_cloud_run_jobs()

        # 3. Verificar Cloud Schedulers
        check_cloud_schedulers()

        # 4. Supabase
        check_supabase()

        # 5. OpenAI
        check_openai()

        # 6. Anthropic
        check_anthropic()

        # 7. Resend
        check_resend()

        # Resumen
        print("\n" + "=" * 60, flush=True)
        print(f"RESUMEN: {len(problemas)} problema(s), {len(reparaciones)} reparación(es)", flush=True)
        for p in problemas:
            print(f"  {p}", flush=True)
        for r in reparaciones:
            print(f"  {r}", flush=True)
        print("=" * 60, flush=True)

        # 8. Enviar email
        html = generar_html_alerta()
        enviar_alerta(html)

    except Exception as e:
        print(f"\n❌ ERROR FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
