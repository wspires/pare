kubectl apply -f k8s/ && kubectl rollout status deployment/shop-api -n shop --timeout=90s && kubectl get pods -n shop -o wide
