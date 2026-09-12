#!/usr/bin/env bash
# 第三套环境（124.16.138.60/61/62）现状盘点脚本
#
# 用法：在每台节点上以普通用户执行，需要 root 的项会自动尝试 sudo；
#       没有 root 也能跑完，拿不到的项标成 N/A 而不是中断。
#
#   bash inventory.sh > inventory-$(hostname).txt 2>&1
#
# 只读：不安装、不修改、不启动任何服务。
set -u

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then SUDO="sudo -n"; fi
fi

sec() { printf '\n========== %s ==========\n' "$1"; }
run() { printf '\n--- $ %s\n' "$*"; eval "$@" 2>&1 | head -80 || echo "(失败/不可用)"; }
has() { command -v "$1" >/dev/null 2>&1; }

sec "0 采集元信息"
run 'date -u "+%Y-%m-%dT%H:%M:%SZ"'
run 'hostname -f; hostname -I 2>/dev/null || hostname -i'
run 'id'
echo "sudo 免密: $([ -n "$SUDO" ] && echo yes || echo '否/需要密码')"

sec "1 操作系统与内核"
run 'cat /etc/os-release'
run 'uname -a'
run 'uptime'
run 'timedatectl 2>/dev/null || (date; cat /etc/timezone 2>/dev/null)'

sec "2 时间同步"
run 'timedatectl show -p NTPSynchronized -p NTP -p TimeUSec 2>/dev/null'
run 'systemctl is-active chronyd chrony systemd-timesyncd ntpd 2>/dev/null'
run 'chronyc tracking 2>/dev/null || ntpq -p 2>/dev/null'

sec "3 硬件与可用资源"
run 'nproc; lscpu | head -25'
run 'free -h'
run 'df -hT | grep -v tmpfs'
run 'lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT 2>/dev/null'

sec "4 容器运行时"
for c in docker containerd crictl ctr nerdctl podman; do
  printf '%-10s ' "$c"; has $c && command -v $c || echo "(未安装)"
done
run 'docker version 2>/dev/null | head -20'
run 'docker info 2>/dev/null | egrep -i "Server Version|Storage Driver|Cgroup|Runtimes|Registry|Insecure"'
run 'containerd --version 2>/dev/null'
run "$SUDO crictl version 2>/dev/null"
run "$SUDO crictl info 2>/dev/null | head -40"

sec "5 Kubernetes 存在性"
for c in kubectl kubeadm kubelet k3s k0s minikube kind helm; do
  printf '%-10s ' "$c"; has $c && ($c version --short 2>/dev/null || $c version 2>/dev/null | head -2 || command -v $c) || echo "(未安装)"
done
run 'systemctl is-active kubelet k3s k3s-agent 2>/dev/null'
run 'ls -la /etc/kubernetes/ 2>/dev/null'
run 'ls -la /etc/kubernetes/manifests/ 2>/dev/null'
run 'ls -la ~/.kube/ 2>/dev/null'
run "$SUDO ls -la /root/.kube/ 2>/dev/null"
run "$SUDO ls -la /etc/rancher/k3s/ 2>/dev/null"

sec "6 集群状态（有 kubeconfig 才有输出）"
run 'kubectl version 2>/dev/null'
run 'kubectl get nodes -o wide 2>/dev/null'
run 'kubectl get nodes -o custom-columns="NAME:.metadata.name,ROLES:.metadata.labels.node-role\.kubernetes\.io/control-plane,KUBELET:.status.nodeInfo.kubeletVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion,OS:.status.nodeInfo.osImage" 2>/dev/null'
run 'kubectl get ns 2>/dev/null'
run 'kubectl get sc 2>/dev/null'
run 'kubectl get pods -A -o wide 2>/dev/null'
run 'kubectl get deploy,sts,ds -A 2>/dev/null'
run 'kubectl get pvc,pv -A 2>/dev/null'
run 'kubectl top nodes 2>/dev/null'
run 'kubectl api-resources 2>/dev/null | egrep -i "chaosblade|chaos-mesh|chaosmesh|podchaos|networkchaos|stresschaos|coroot"'
run 'kubectl get crd 2>/dev/null | egrep -i "chaos|coroot|otel|opentelemetry|prometheus|monitoring"'
run 'helm list -A 2>/dev/null'
run 'kubectl auth can-i --list 2>/dev/null | head -30'

sec "7 网络插件与集群网络"
run 'ls /etc/cni/net.d/ 2>/dev/null'
run 'cat /etc/cni/net.d/*.conflist 2>/dev/null | head -60'
run 'ls /opt/cni/bin/ 2>/dev/null'
run 'ip -br addr'
run 'ip route'
run 'kubectl -n kube-system get pods 2>/dev/null | egrep -i "calico|flannel|cilium|weave|kube-proxy|coredns"'

sec "8 DNS 与出网"
run 'cat /etc/resolv.conf'
run 'getent hosts registry-1.docker.io'
run 'getent hosts github.com'
for u in https://registry-1.docker.io/v2/ https://github.com https://ghcr.io/v2/ https://quay.io/v2/ https://k8s.gcr.io https://registry.k8s.io/v2/ https://pypi.org/simple/ https://mirrors.aliyun.com; do
  printf '  %-40s ' "$u"
  curl -sk -o /dev/null -m 8 -w 'http=%{http_code} time=%{time_total}s\n' "$u" 2>&1 || echo "失败"
done
run 'env | egrep -i "proxy" || echo "(无代理环境变量)"'
run 'cat /etc/docker/daemon.json 2>/dev/null'

sec "9 内核与安全（ChaosBlade/AppArmor 相关）"
run 'stat -fc %T /sys/fs/cgroup && echo "cgroup2fs=v2, tmpfs=v1"'
run 'cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null'
run "$SUDO aa-status 2>/dev/null | head -30 || echo '(apparmor 未安装或无权限)'"
run 'ls /etc/apparmor.d/ 2>/dev/null | head -40'
run 'getenforce 2>/dev/null || echo "(无 SELinux)"'
run 'sysctl net.ipv4.ip_forward net.bridge.bridge-nf-call-iptables 2>/dev/null'
run 'lsmod 2>/dev/null | egrep "br_netfilter|overlay|ip_vs" '
run 'swapon --show; grep -c swap /etc/fstab'

sec "10 端口占用与已有服务"
run "$SUDO ss -lntp 2>/dev/null | head -50 || ss -lnt | head -50"
run 'systemctl list-units --type=service --state=running 2>/dev/null | head -50'

sec "11 已有的相关制品"
run 'ls -la /opt /srv /data 2>/dev/null'
run 'ls -la /var/lib/resbench-stage2 2>/dev/null || echo "(无平台数据目录)"'
run 'docker images 2>/dev/null | head -30'
run "$SUDO crictl images 2>/dev/null | head -30"
run 'which python3 python3.12 uv git; python3 --version 2>/dev/null'

sec "12 节点间互通"
for ip in 124.16.138.60 124.16.138.61 124.16.138.62; do
  printf '  ping %-16s ' "$ip"; ping -c1 -W2 "$ip" >/dev/null 2>&1 && echo OK || echo 不通
done

echo
echo "========== 盘点结束 =========="

# ---------------------------------------------------------------------------
# 机器可读事实：附在人类可读报告之后，供 gap_report.py 生成差距表。
# 每行 key=value，取不到的一律 value=unknown（不猜、不留空）。
# ---------------------------------------------------------------------------
emit() { printf 'FACT %s=%s\n' "$1" "${2:-unknown}"; }
val()  { v="$($* 2>/dev/null | head -1 | tr -d '\r')"; [ -n "$v" ] && printf '%s' "$v" || printf 'unknown'; }

echo
echo "========== FACTS =========="
emit host           "$(hostname -f 2>/dev/null || hostname)"
emit os             "$(. /etc/os-release 2>/dev/null && printf '%s %s' "$ID" "$VERSION_ID")"
emit kernel         "$(uname -r)"
emit cpu_cores      "$(nproc 2>/dev/null)"
emit mem_total_mb   "$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)"
emit disk_root_avail "$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')"
emit cgroup_version "$(stat -fc %T /sys/fs/cgroup 2>/dev/null | sed 's/cgroup2fs/v2/; s/tmpfs/v1/')"
emit swap_on        "$(swapon --show --noheadings 2>/dev/null | head -1 >/dev/null && echo yes || echo no)"
emit time_synced    "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"

for c in docker containerd crictl kubectl kubeadm helm; do
  if command -v "$c" >/dev/null 2>&1; then emit "has_$c" yes; else emit "has_$c" no; fi
done
emit docker_version    "$(docker version --format '{{.Server.Version}}' 2>/dev/null)"
emit kubelet_active    "$(systemctl is-active kubelet 2>/dev/null)"
emit k8s_server        "$(kubectl version -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["serverVersion"]["gitVersion"])' 2>/dev/null)"
emit k8s_nodes         "$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
emit k8s_runtime       "$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null)"
emit storageclass      "$(kubectl get sc --no-headers 2>/dev/null | awk '{print $1}' | paste -sd, -)"
emit cni               "$(ls /etc/cni/net.d/ 2>/dev/null | paste -sd, -)"
emit apparmor_profile  "$(grep -c '^resbench-agent-runtime' /sys/kernel/security/apparmor/profiles 2>/dev/null)"

# Take the exit status of kubectl itself. Piping into `head` and testing that
# always reports success, which made every component look present.
present_if() { if "$@" >/dev/null 2>&1; then printf present; else printf absent; fi; }
for ns in otel-demo observability coroot chaos-mesh resiliencebenchmark-system; do
  emit "ns_$ns" "$(present_if kubectl get ns "$ns" -o name)"
done
emit chaosblade_operator "$(present_if kubectl -n default get deploy chaosblade-operator -o name)"
emit chaosblade_wrapper  "$(present_if kubectl -n default get cm chaosblade-cgroupns-wrapper -o name)"

for u in https://registry-1.docker.io/v2/ https://ghcr.io/v2/ https://quay.io/v2/ https://registry.k8s.io/v2/; do
  code="$(curl -sk -o /dev/null -m 8 -w '%{http_code}' "$u" 2>/dev/null)"
  emit "egress_$(printf '%s' "$u" | sed -E 's#https://([^/]+)/.*#\1#; s/[.:]/_/g')" "${code:-000}"
done
emit egress_old_harbor "$(curl -s -o /dev/null -m 8 -w '%{http_code}' http://1.94.151.57:85/v2/ 2>/dev/null)"
echo "========== END FACTS =========="
