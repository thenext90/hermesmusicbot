#!/usr/bin/env bash
# ── HermesMusicBot · verificación rápida de un contenedor ───────────────────
# Corre la prueba de humo contra el bot en Docker y revisa el audio del contenedor.
#
#   ./tests/docker_smoke.sh                       # contra http://127.0.0.1:8080
#   BASE_URL=http://IP:8080 API_TOKEN=xxx ./tests/docker_smoke.sh
#   COMPOSE="docker compose -f docker-compose.vps.yml" ./tests/docker_smoke.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
COMPOSE="${COMPOSE:-docker compose}"
PY="${PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3

fallos=0

echo "── 1. ¿el contenedor está arriba? ──"
if $COMPOSE ps --status running 2>/dev/null | grep -q musicbot; then
    echo "  ✅ contenedor corriendo"
else
    echo "  ⚠️  no veo el contenedor corriendo (¿lo levantaste?)"
fi

echo
echo "── 2. ¿la API responde? ──"
if curl -fsS "${BASE_URL}/" >/dev/null 2>&1; then
    echo "  ✅ ${BASE_URL}/ responde"
else
    echo "  ❌ ${BASE_URL}/ no responde"
    fallos=$((fallos + 1))
fi

echo
echo "── 3. salida de audio del contenedor ──"
if $COMPOSE exec -T musicbot pactl info >/dev/null 2>&1; then
    $COMPOSE exec -T musicbot pactl list short sinks 2>/dev/null | sed 's/^/  /'
    echo "  ✅ hay servidor de audio dentro del contenedor"
else
    echo "  · el contenedor no tiene PulseAudio propio (esperado en modo host)"
fi

echo
echo "── 4. prueba de humo de punta a punta ──"
# Mejor dentro del contenedor: ahí httpx ya está instalado (un clon nuevo no tiene venv).
if $COMPOSE exec -T musicbot sh -c 'true' >/dev/null 2>&1; then
    echo "  (corriendo dentro del contenedor)"
    $COMPOSE exec -T -e BASE_URL=http://127.0.0.1:8080 -e API_TOKEN="${API_TOKEN:-}" \
        musicbot python tests/smoke_test.py
else
    echo "  (corriendo en el host, contra ${BASE_URL})"
    BASE_URL="$BASE_URL" API_TOKEN="${API_TOKEN:-}" "$PY" tests/smoke_test.py
fi
[[ $? -eq 0 ]] || fallos=$((fallos + 1))

echo
if [[ $fallos -eq 0 ]]; then
    echo "✅ Contenedor verificado"
else
    echo "❌ $fallos bloque(s) con problemas — revisa: $COMPOSE logs --tail=50 musicbot"
    exit 1
fi
