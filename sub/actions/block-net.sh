#!/bin/bash
# block-net.sh — 用 cgroup v2 + iptables 给 HCA 和 provjobd 断网/恢复
# 用法:
#   block-net.sh block    # 断网
#   block-net.sh undo     # 恢复
#   block-net.sh status   # 查看状态

CGROUP_NAME="no_net"
CGROUP_PATH="/sys/fs/cgroup/${CGROUP_NAME}"
IPTABLES_RULE="-m cgroup --path ${CGROUP_NAME} -j DROP"
PATTERNS=("hosted-compute-agent" "provjobd")

find_pids() {
    for path in "${PATTERNS[@]}"; do
        pgrep -f "$path" 2>/dev/null || true
    done | sort -u
}

cmd_block() {
    if [ ! -d "$CGROUP_PATH" ]; then
        mkdir "$CGROUP_PATH"
        echo "[+] cgroup created: $CGROUP_PATH"
    else
        echo "[=] cgroup already exists"
    fi

    local moved=0 already=0
    for pid in $(find_pids); do
        current=$(cat /proc/$pid/cgroup 2>/dev/null | sed 's/^0:://')
        comm=$(cat /proc/$pid/comm 2>/dev/null)
        if [ "$current" = "/${CGROUP_NAME}" ]; then
            echo "[=] PID $pid already blocked ($comm)"
            already=$((already + 1))
        else
            if echo "$pid" > "$CGROUP_PATH/cgroup.procs" 2>/dev/null; then
                echo "[+] PID $pid -> no_net ($comm)"
                moved=$((moved + 1))
            else
                echo "[-] PID $pid move failed ($comm)"
            fi
        fi
    done
    echo "[i] moved=$moved already=$already"

    if iptables -C OUTPUT $IPTABLES_RULE 2>/dev/null; then
        echo "[=] iptables rule already exists"
    else
        iptables -A OUTPUT $IPTABLES_RULE
        echo "[+] iptables rule added"
    fi
    echo ""
    echo "=== DONE: HCA & provjobd network blocked ==="
}

cmd_undo() {
    if iptables -C OUTPUT $IPTABLES_RULE 2>/dev/null; then
        iptables -D OUTPUT $IPTABLES_RULE
        echo "[+] iptables rule removed"
    else
        echo "[=] iptables rule not found"
    fi

    local restored=0
    if [ -d "$CGROUP_PATH" ]; then
        for pid in $(cat "$CGROUP_PATH/cgroup.procs" 2>/dev/null); do
            if echo "$pid" > /sys/fs/cgroup/cgroup.procs 2>/dev/null; then
                echo "[+] PID $pid restored"
                restored=$((restored + 1))
            fi
        done
    fi
    echo "[i] restored=$restored"

    if [ -d "$CGROUP_PATH" ]; then
        rmdir "$CGROUP_PATH" 2>/dev/null && echo "[+] cgroup removed" || echo "[-] cgroup not empty, kept"
    fi
    echo ""
    echo "=== DONE: network restored ==="
}

cmd_status() {
    echo "=== cgroup ==="
    if [ -d "$CGROUP_PATH" ]; then
        echo "  path: $CGROUP_PATH"
        for pid in $(cat "$CGROUP_PATH/cgroup.procs" 2>/dev/null); do
            echo "    PID $pid: $(cat /proc/$pid/comm 2>/dev/null)"
        done
    else
        echo "  not created"
    fi
    echo ""
    echo "=== iptables ==="
    if iptables -C OUTPUT $IPTABLES_RULE 2>/dev/null; then
        echo "  ACTIVE (blocking)"
    else
        echo "  not set"
    fi
    echo ""
    echo "=== target processes ==="
    for pat in "${PATTERNS[@]}"; do
        pgrep -af "$pat" 2>/dev/null | while read -r line; do
            pid=$(echo "$line" | awk '{print $1}')
            cg=$(cat /proc/$pid/cgroup 2>/dev/null | sed 's/^0:://')
            echo "  $line  [cgroup: $cg]"
        done
    done
}

case "${1:-status}" in
    block|on)  cmd_block ;;
    undo|off)  cmd_undo ;;
    status)    cmd_status ;;
    *) echo "Usage: $0 {block|undo|status}"; exit 1 ;;
esac
