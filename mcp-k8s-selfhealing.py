import uvicorn
import re
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from kubernetes import client, config
from typing import Optional

app = FastAPI(title="Elastic-K8s Unified Self-Healing MCP")
API_TOKEN = "TOKEN"

try:
    config.load_kube_config()
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()
    print("✅ Kubernetes API Connected.")
except Exception as e:
    print(f"❌ K8s Connection Failed: {e}")

def parse_memory_to_mib(mem_str: str) -> int:
    units = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "K": 1000/(1024**2), "M": 1000/1024, "G": 1000}
    match = re.match(r"(\d+)([a-zA-Z]*)", str(mem_str))
    return int(int(match.group(1)) * units.get(match.group(2), 1)) if match else 256

def resolve_to_deployment(name: str, namespace: str) -> str:
    """Resolves Pod -> ReplicaSet -> Deployment hierarchy"""
    try:
        pod = core_v1.read_namespaced_pod(name=name, namespace=namespace)
        for owner in pod.metadata.owner_references:
            if owner.kind == "ReplicaSet":
                rs = apps_v1.read_namespaced_replica_set(name=owner.name, namespace=namespace)
                for rs_owner in rs.metadata.owner_references:
                    if rs_owner.kind == "Deployment":
                        return rs_owner.name
        return name
    except:
        return name

class ManageRequest(BaseModel):
    action: str             # "increment_memory" or "scale"
    target: str             # Name of the target pod or deployment
    namespace: str = "default"
    # Note: 'replicas' has been removed. The logic now lives entirely within the MCP.

@app.post("/manage")
async def manage_deployment(request: ManageRequest, authorization: str = Header(None)):
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Resolve target name (in case Elastic sends a Pod name instead of a Deployment)
        target_deploy = resolve_to_deployment(request.target, request.namespace)
        
        # ----------------------------------------------------------------
        # SCENARIO A: Vertical Scaling - Memory Increase (+25%)
        # ----------------------------------------------------------------
        if request.action == "increment_memory":
            deploy = apps_v1.read_namespaced_deployment(name=target_deploy, namespace=request.namespace)
            container = deploy.spec.template.spec.containers[0]
            current_limit = container.resources.limits.get("memory", "256Mi")
            
            new_mib = int(parse_memory_to_mib(current_limit) * 1.25)
            new_limit_str = f"{new_mib}Mi"

            patch = {"spec": {"template": {"spec": {"containers": [{"name": container.name, "resources": {"limits": {"memory": new_limit_str}}}]}}}}
            apps_v1.patch_namespaced_deployment(name=target_deploy, namespace=request.namespace, body=patch)
            
            return {"status": "success", "message": f"Vertical scaling: {target_deploy} increased to {new_limit_str}."}

        # ----------------------------------------------------------------
        # SCENARIO B: Horizontal Scaling - High Availability (SRE Logic)
        # ----------------------------------------------------------------
        elif request.action == "scale":
            # 1. Fetch the current state from the Kubernetes cluster
            deploy = apps_v1.read_namespaced_deployment(name=target_deploy, namespace=request.namespace)
            current_replicas = deploy.spec.replicas or 1
            
            # 2. SRE Logic: If strictly running 1 replica, scale to 2 (HA). If already >= 2, just add 1.
            new_replicas = current_replicas + 1 if current_replicas >= 2 else 2

            # 3. Apply the scaling patch to the cluster
            scale_patch = {"spec": {"replicas": new_replicas}}
            apps_v1.patch_namespaced_deployment_scale(name=target_deploy, namespace=request.namespace, body=scale_patch)
            
            # Return a detailed audit message for the Kibana Dashboard
            return {"status": "success", "message": f"Horizontal scaling: {target_deploy} scaled from {current_replicas} to {new_replicas} replicas."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)