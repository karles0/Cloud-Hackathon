import os
import sys
import requests
import time

# ---------------------------------------------------------
# Configuración
# ---------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL")

def test_pipeline_completo(file_path):
    if not BASE_URL:
        print("Error: La variable 'BASE_URL' no está configurada.")
        return

    print(f"🚀 Iniciando prueba completa: '{file_path}'")

    # 1. Obtener URL de subida
    print("1. Solicitando URL de subida...")
    resp = requests.post(f"{BASE_URL}/api/v1/manuscripts/upload-url", json={"fileName": os.path.basename(file_path)})
    data = resp.json()
    manuscript_id = data["manuscriptId"]
    upload_url = data["uploadUrl"]

    # 2. Subir archivo a S3
    print(f"2. Subiendo archivo a S3 (ID: {manuscript_id})...")
    with open(file_path, "rb") as f:
        requests.put(upload_url, data=f, headers={"Content-Type": "application/pdf"})
    
    sleep_time = 30
    print(f"   Subida exitosa. Esperando procesamiento ({sleep_time}s)...")
    time.sleep(sleep_time)

    # 3. Consultar Estado (GET /manuscripts/{id})
    print("3. Consultando estado del manuscrito...")
    status_resp = requests.get(f"{BASE_URL}/api/v1/manuscripts/{manuscript_id}")
    status_json = status_resp.json()
    print(f"   Estado: {status_json.get('status', 'PENDING')}")
    print(f"   Progreso: {status_json.get('progress')}")

    # 4. Consultar Resultados (GET /manuscripts/{id}/results)
    print("4. Consultando resultados...")
    res_resp = requests.get(f"{BASE_URL}/api/v1/manuscripts/{manuscript_id}/results")
    resultados = res_resp.json()
    print(f"   Referencias evaluadas: {resultados.get('totalEvaluated', 0)}")
    print(f"   Citas zombi detectadas: {resultados.get('zombieCount', 0)}")
    
    print("\n✅ Prueba finalizada correctamente.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: BASE_URL='...' python test_upload.py <archivo.pdf>")
        sys.exit(1)
        
    test_pipeline_completo(sys.argv[1])
