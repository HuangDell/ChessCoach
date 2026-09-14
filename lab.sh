#!/bin/bash

PROXY_URL="http://172.31.160.1:7890"

proxy_status() {
    if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ] || [ -n "${ALL_PROXY:-}" ] || [ -n "${CODEX_HOME:-}" ] ; then
        echo "Proxy is enabled:"
        echo "  CODEX_HOME=${CODEX_HOME:-}"
    else
        echo "Proxy is disabled."
    fi
}

proxy_on() {
    # export HTTP_PROXY="${PROXY_URL}"
    # export HTTPS_PROXY="${PROXY_URL}"
    # export ALL_PROXY="${PROXY_URL}"
    export CODEX_HOME="${PWD}/.codex"
    proxy_status
}

proxy_off() {
    unset HTTP_PROXY
    unset HTTPS_PROXY
    unset ALL_PROXY
    unset http_proxy
    unset https_proxy
    unset all_proxy
    proxy_status
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "Please source this script so it can update the current shell:"
    echo "  source ./proxy.sh on"
    echo "  source ./proxy.sh off"
    echo "  source ./proxy.sh status"
    exit 1
fi

case "${1:-status}" in
    on|enable|start)
        proxy_on
        ;;
    off|disable|stop)
        proxy_off
        ;;
    status)
        proxy_status
        ;;
    *)
        echo "Usage: source ./proxy.sh {on|off|status}"
        return 1
        ;;
esac
