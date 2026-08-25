#!/bin/sh
set -eu

IMAGE="${RESBENCH_SEMANTIC_SCAN_IMAGE:-1.94.151.57:85/observe/resbench-semantic-scan:otel-2.2.0-semantic-v8@sha256:34927e2d1f92efa4623fff565265fd64d30e0aaa6d90d7616a8e634f13b4ecd3}"
NODE_NAME="${RESBENCH_NODE_NAME:-}"
PIPELINE_EXECUTE="${RESBENCH_PIPELINE_EXECUTE:-true}"
QUALIFICATION_ONLY="${RESBENCH_QUALIFICATION_ONLY:-false}"
case "$IMAGE" in
  *[!A-Za-z0-9._/:@-]*|'') echo "invalid image reference" >&2; exit 2 ;;
esac
case "$NODE_NAME" in
  *[!A-Za-z0-9.-]*) echo "invalid Kubernetes node name" >&2; exit 2 ;;
esac
case "$PIPELINE_EXECUTE" in
  true) EXECUTE_ARG="            - --execute" ;;
  false) EXECUTE_ARG="" ;;
  *) echo "RESBENCH_PIPELINE_EXECUTE must be true or false" >&2; exit 2 ;;
esac
case "$QUALIFICATION_ONLY" in
  true) QUALIFICATION_ARG="            - --qualification-only" ;;
  false) QUALIFICATION_ARG="" ;;
  *) echo "RESBENCH_QUALIFICATION_ONLY must be true or false" >&2; exit 2 ;;
esac
NODE_SELECTOR=""
if [ -n "$NODE_NAME" ]; then
  NODE_SELECTOR="      nodeSelector:
        kubernetes.io/hostname: ${NODE_NAME}"
fi

cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: resbench-semantic-scan
  namespace: resbench-system
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: resbench-semantic-scan
${NODE_SELECTOR}
      containers:
        - name: semantic-scan
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          envFrom:
            - secretRef:
                name: resbench-semantic-scan-model
                optional: true
          env:
            - name: RESBENCH_CODEGRAPH_COMMAND
              value: /usr/local/bin/codegraph
          args:
${EXECUTE_ARG}
${QUALIFICATION_ARG}
            - --run-root
            - /data/mj/resbench-runs
            - --system-root
            - /data/mj/resbench-system
            - --repository-url
            - https://github.com/open-telemetry/opentelemetry-demo.git
            - --revision
            - 2.2.0
            - --expected-commit
            - b74a7bc7bbe66099c61951f42b24dab8b6f02d18
            - --kubeconfig-ref
            - /data/mj/resbench-system/kubeconfig
          volumeMounts:
            - name: resbench-system
              mountPath: /data/mj/resbench-system
            - name: resbench-runs
              mountPath: /data/mj/resbench-runs
      volumes:
        - name: resbench-system
          hostPath:
            path: /data/mj/resbench-system
            type: DirectoryOrCreate
        - name: resbench-runs
          hostPath:
            path: /data/mj/resbench-runs
            type: DirectoryOrCreate
EOF
