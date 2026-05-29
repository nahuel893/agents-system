import os
import json
import time
import urllib.request
import urllib.error

# Config
API_TOKEN = "ol_api_pB0Gy2EHCP4w6uXWexL2M9ZSPqNGilacvN2kcJ"
BASE_URL = "https://servidor-net.tail65a83a.ts.net"
WORKSPACE_DIR = "/home/nahuel/agents-system-D-003"

def make_request(path, data=None):
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    req_body = json.dumps(data).encode("utf-8") if data is not None else None
    
    max_retries = 6
    backoff = 3
    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                res_body = res.read().decode("utf-8")
                return json.loads(res_body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"Rate limited (429) on {path}. Retrying in {backoff} seconds (Attempt {attempt+1}/{max_retries})...")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"HTTP Error {e.code} for {path}: {e.read().decode('utf-8')}")
            raise e
        except Exception as e:
            print(f"Connection Error for {path}: {e}")
            raise e
            
    raise Exception(f"Failed after {max_retries} retries due to rate limiting on {path}")

def read_file(filepath):
    full_path = os.path.join(WORKSPACE_DIR, filepath)
    if not os.path.exists(full_path):
        print(f"Warning: File {full_path} not found!")
        return f"*File {filepath} not found on disk.*"
    with open(full_path, "r", encoding="utf-8") as f:
        return f.read()

def create_document(title, text, collection_id, parent_id=None):
    payload = {
        "title": title,
        "text": text,
        "collectionId": collection_id,
        "publish": True
    }
    if parent_id:
        payload["parentDocumentId"] = parent_id
        
    print(f"Creating document: '{title}' (parent: {parent_id})...")
    res = make_request("/api/documents.create", payload)
    doc_id = res["data"]["id"]
    print(f"Created doc '{title}' with ID: {doc_id}")
    time.sleep(1.2)  # Pace the requests to prevent hitting rate limits
    return doc_id

def main():
    print("--- Listing Collections ---")
    collections_res = make_request("/api/collections.list", {})
    collections = collections_res.get("data", [])
    
    col_id = None
    for col in collections:
        print(f"Found Collection: {col['name']} ({col['id']})")
        if col['name'] == "Agent Platform":
            col_id = col['id']
            
    if col_id:
        print(f"Reusing existing 'Agent Platform' collection: {col_id}")
    else:
        print("Creating 'Agent Platform' collection...")
        create_res = make_request("/api/collections.create", {
            "name": "Agent Platform",
            "color": "#3b82f6",
            "permission": "read_write"
        })
        col_id = create_res["data"]["id"]
        print(f"Created collection with ID: {col_id}")
        
    # 1. Overview (English)
    overview_text = read_file("docs/architecture/agent-platform.md")
    overview_id = create_document("Agent Platform — Overview", overview_text, col_id)
    
    # 1b. Descripción General (Español)
    overview_es_text = read_file("docs/architecture_es/agent-platform.md")
    overview_es_id = create_document("Agent Platform — Descripción General (Español)", overview_es_text, col_id)
    
    # 2. Platform Primitives (parent)
    primitives_intro = (
        "# Platform Primitives\n\n"
        "Esta sección define los bloques básicos y las primitivas fundamentales de la plataforma de agentes:\n\n"
        "- **Manifesto**: Los principios rectores y la filosofía de diseño de la arquitectura.\n"
        "- **Role**: La especificación declarativa de la identidad y límites de un agente.\n"
        "- **Tool**: Los conectores ejecutables con efectos secundarios en sistemas externos.\n"
        "- **Skill**: Los paquetes de comportamiento que guían el razonamiento de los agentes.\n"
        "- **Harness**: El soporte de infraestructura y ciclo de vida de los agentes.\n"
        "- **Policy**: Las políticas de autonomía, delegación y escalamiento.\n"
        "- **Deployment Model**: La topología y estrategias de despliegue."
    )
    primitives_id = create_document("Platform Primitives", primitives_intro, col_id)
    
    # 3. Children of Platform Primitives
    primitives_docs = [
        ("Manifesto", "docs/platform/manifesto.md"),
        ("Role", "docs/platform/role.md"),
        ("Tool", "docs/platform/tool.md"),
        ("Skill", "docs/platform/skill.md"),
        ("Harness", "docs/platform/harness.md"),
        ("Policy", "docs/platform/policy.md"),
        ("Deployment Model", "docs/platform/deployment.md")
    ]
    for title, path in primitives_docs:
        text = read_file(path)
        create_document(title, text, col_id, primitives_id)

    # 3b. Primitivas de la Plataforma (Español) (parent)
    primitives_es_intro = (
        "# Primitivas de la Plataforma (Español)\n\n"
        "Esta sección define los bloques básicos y las primitivas fundamentales de la plataforma de agentes en español:\n\n"
        "- **Manifesto (Español)**: Los principios rectores y la filosofía de diseño de la arquitectura.\n"
        "- **Role (Español)**: La especificación declarativa de la identidad y límites de un agente.\n"
        "- **Tool (Español)**: Los conectores ejecutables con efectos secundarios en sistemas externos.\n"
        "- **Skill (Español)**: Los paquetes de comportamiento que guían el razonamiento de los agentes.\n"
        "- **Harness (Español)**: El soporte de infraestructura y ciclo de vida de los agentes.\n"
        "- **Policy (Español)**: Las políticas de autonomía, delegación y escalamiento (con los límites del cargador).\n"
        "- **Deployment Model (Español)**: La topología, estrategias de despliegue, directivas de mezcla e invariantes."
    )
    primitives_es_id = create_document("Primitivas de la Plataforma (Español)", primitives_es_intro, col_id)
    
    # 3c. Hijos de Primitivas de la Plataforma (Español)
    primitives_es_docs = [
        ("Manifesto (Español)", "docs/platform_es/manifesto.md"),
        ("Role (Español)", "docs/platform_es/role.md"),
        ("Tool (Español)", "docs/platform_es/tool.md"),
        ("Skill (Español)", "docs/platform_es/skill.md"),
        ("Harness (Español)", "docs/platform_es/harness.md"),
        ("Policy (Español)", "docs/platform_es/policy.md"),
        ("Deployment Model (Español)", "docs/platform_es/deployment.md")
    ]
    for title, path in primitives_es_docs:
        text = read_file(path)
        create_document(title, text, col_id, primitives_es_id)
        
    # 4. Architecture (parent)
    arch_intro = (
        "# Architecture\n\n"
        "Documentación formal sobre las políticas de seguridad, permisos y control de flujo del sistema multi-agente:\n\n"
        "- **Delegation Policy**: Las reglas explícitas de spawning y control de agentes hijos.\n"
        "- **Permission Model**: La matriz de control de acceso basada en roles (RBAC) y revalidación en caliente."
    )
    arch_id = create_document("Architecture", arch_intro, col_id)
    
    # 5. Children of Architecture
    arch_docs = [
        ("Delegation Policy", "docs/architecture/delegation-policy.md"),
        ("Permission Model", "docs/architecture/permission-model.md")
    ]
    for title, path in arch_docs:
        text = read_file(path)
        create_document(title, text, col_id, arch_id)

    # 5b. Arquitectura (Español) (parent)
    arch_es_intro = (
        "# Arquitectura (Español)\n\n"
        "Documentación formal sobre las políticas de seguridad, permisos y control de flujo del sistema multi-agente en español:\n\n"
        "- **Política de Delegación (Español)**: Las reglas explícitas de spawning y control de agentes hijos.\n"
        "- **Modelo de Permisos (Español)**: La matriz de control de acceso basada en roles (RBAC) y revalidación en caliente."
    )
    arch_es_id = create_document("Arquitectura (Español)", arch_es_intro, col_id)
    
    # 5c. Hijos de Arquitectura (Español)
    arch_es_docs = [
        ("Política de Delegación (Español)", "docs/architecture_es/delegation-policy.md"),
        ("Modelo de Permisos (Español)", "docs/architecture_es/permission-model.md")
    ]
    for title, path in arch_es_docs:
        text = read_file(path)
        create_document(title, text, col_id, arch_es_id)
        
    # 6. Delivery (parent)
    delivery_intro = (
        "# Delivery\n\n"
        "Especificaciones del alcance comprometido y las integraciones concretas para los despliegues de clientes:\n\n"
        "- **BADIE Seller AI**: El bot de toma de pedidos conversacional por WhatsApp para Distribuidora BADIE S.A."
    )
    delivery_id = create_document("Delivery", delivery_intro, col_id)
    
    # 7. Children of Delivery
    create_document("BADIE Seller AI", read_file("docs/delivery/badie-seller-ai.md"), col_id, delivery_id)

    # 7b. Entrega (Español) (parent)
    delivery_es_intro = (
        "# Entrega (Español)\n\n"
        "Especificaciones del alcance comprometido y las integraciones concretas para los despliegues de clientes en español:\n\n"
        "- **Alcance: BADIE Seller AI (Español)**: El bot de toma de pedidos conversacional por WhatsApp para Distribuidora BADIE S.A."
    )
    delivery_es_id = create_document("Entrega (Español)", delivery_es_intro, col_id)
    
    # 7c. Hijos de Entrega (Español)
    create_document("Alcance: BADIE Seller AI (Español)", read_file("docs/delivery_es/badie-seller-ai.md"), col_id, delivery_es_id)
    
    # 8. Agent Definitions (parent)
    defs_intro = (
        "# Agent Definitions\n\n"
        "En esta sección se definen los agentes concretos operados en el ecosistema. La plataforma está estructurada en dos niveles:\n\n"
        "1. **Platform/Roles (Genéricos)**: Definiciones base abstractas reutilizables en cualquier despliegue.\n"
        "2. **Deployments/{client} (Overrides)**: Las especificaciones, manifiestos y políticas ajustadas y extendidas para clientes específicos."
    )
    defs_id = create_document("Agent Definitions", defs_intro, col_id)
    
    # 9. Generic Roles (child of defs)
    generic_intro = (
        "# Generic Roles\n\n"
        "Índice de los roles de agente base abstractos definidos por la plataforma Core:\n\n"
        "- **sales-agent**: Agente orientado a la interacción con clientes y toma de pedidos.\n"
        "- **orchestrator**: Orquestador principal encargado del ciclo de vida y ruteo.\n"
        "- **data-agent**: Agente de consulta y análisis de datos.\n"
        "- **summary-agent**: Agente de generación de resúmenes y consolidación."
    )
    generic_roles_id = create_document("Generic Roles", generic_intro, col_id, defs_id)
    
    # 10. Children of Generic Roles
    roles = ["sales-agent", "orchestrator", "data-agent", "summary-agent"]
    for role in roles:
        role_text = read_file(f"platform/roles/{role}/role.md")
        manifest_text = read_file(f"platform/roles/{role}/manifest.md")
        policy_text = read_file(f"platform/roles/{role}/policy.md")
        
        combined_text = (
            f"# Role: {role}\n\n"
            f"## Role Description\n\n{role_text}\n\n"
            f"## Manifest\n\n{manifest_text}\n\n"
            f"## Policy\n\n{policy_text}"
        )
        create_document(role, combined_text, col_id, generic_roles_id)
        
    # 11. BADIE Deployment (child of defs)
    badie_role = read_file("deployments/badie/sales-agent/role.md")
    badie_manifest = read_file("deployments/badie/sales-agent/manifest.md")
    badie_policy = read_file("deployments/badie/sales-agent/policy.md")
    
    badie_combined = (
        "# BADIE Deployment — sales-agent\n\n"
        "Este documento consolida la especificación, manifiesto y políticas sobreescritas para el Agente de Ventas en el entorno real de Distribuidora BADIE S.A.\n\n"
        "## Role Overrides\n\n" + badie_role + "\n\n"
        "## Manifest Overrides\n\n" + badie_manifest + "\n\n"
        "## Policy Overrides\n\n" + badie_policy
    )
    badie_dep_id = create_document("BADIE Deployment — sales-agent", badie_combined, col_id, defs_id)
    
    # 12. BADIE Skills (child of badie_dep)
    skills = ["order_extraction", "colloquial_matching", "confirm_flow"]
    for skill in skills:
        skill_text = read_file(f"deployments/badie/sales-agent/skills/{skill}.md")
        title = f"Skill: {skill}"
        create_document(title, skill_text, col_id, badie_dep_id)

if __name__ == "__main__":
    main()
