#!/usr/bin/env python3
"""Prueba de humo de HermesMusicBot contra una instancia en marcha.

Uso:
    python tests/smoke_test.py                      # contra http://127.0.0.1:8080
    BASE_URL=http://192.168.3.10:8080 API_TOKEN=xxx python tests/smoke_test.py

Verifica el flujo completo: ayuda, play, cola, pausa, reanudar, siguiente,
volumen, detener. Sale con código 1 si algo falla.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.getenv("API_TOKEN", "")
CANCION_1 = os.getenv("CANCION_1", "Toygar Işıklı Jenerik Müziği Fatmagül")
CANCION_2 = os.getenv("CANCION_2", "Daft Punk Get Lucky")

fallos: list[str] = []


def cabeceras() -> dict:
    return {"X-API-Token": TOKEN} if TOKEN else {}


def llamar(texto: str, cliente: httpx.Client) -> dict:
    r = cliente.post(f"{BASE_URL}/command", json={"text": texto, "user": "smoke"}, headers=cabeceras(), timeout=180)
    r.raise_for_status()
    datos = r.json()
    print(f"  » {texto!r:45} → {datos['reply'][:90]}")
    return datos


def comprobar(condicion: bool, mensaje: str) -> None:
    if condicion:
        print(f"    ✅ {mensaje}")
    else:
        print(f"    ❌ {mensaje}")
        fallos.append(mensaje)


def main() -> int:
    with httpx.Client() as cliente:
        print("\n[1] La raíz responde")
        info = cliente.get(f"{BASE_URL}/", timeout=15).json()
        comprobar(info.get("estado") == "ok", f"servicio arriba: {info.get('servicio')}")

        print("\n[2] 'ayuda' devuelve el menú")
        ayuda = llamar("ayuda", cliente)
        comprobar("play" in ayuda["reply"].lower(), "el menú lista los comandos")

        print("\n[3] 'play' reproduce un tema")
        datos = llamar(f"play {CANCION_1}", cliente)
        comprobar(datos["state"]["playing"] is not None, "hay algo sonando")
        time.sleep(4)
        estado = cliente.get(f"{BASE_URL}/state", headers=cabeceras(), timeout=15).json()
        comprobar(estado["mpv_alive"], "el proceso mpv sigue vivo (audio en curso)")
        comprobar(bool(estado["playing"] and estado["playing"]["duration"]), "yt-dlp resolvió metadatos reales del video")
        comprobar(estado["last_error"] is None, f"sin errores de mpv ({estado['last_error']})")

        print("\n[4] Un segundo 'play' se encola")
        datos = llamar(f"play {CANCION_2}", cliente)
        comprobar(datos["state"]["queue_length"] == 1, "la cola tiene 1 tema")

        print("\n[5] 'cola' la muestra")
        datos = llamar("cola", cliente)
        comprobar("Cola" in datos["reply"], "la cola se lista")

        print("\n[6] 'pausa' y 'continúa'")
        pausa = llamar("pausa", cliente)
        comprobar(pausa["state"]["paused"] is True, "quedó en pausa")
        sigue = llamar("continúa", cliente)
        comprobar(sigue["state"]["paused"] is False, "se reanudó")

        print("\n[7] 'volumen 40'")
        datos = llamar("volumen 40", cliente)
        comprobar(datos["state"]["volume"] == 40, "volumen aplicado a 40")

        print("\n[8] 'siguiente' salta de tema")
        datos = llamar("siguiente", cliente)
        comprobar(datos["state"]["playing"] is not None, "está sonando el siguiente")
        comprobar(datos["state"]["queue_length"] == 0, "la cola quedó vacía")

        print("\n[9] 'detente' para y limpia")
        datos = llamar("detente", cliente)
        comprobar(datos["state"]["playing"] is None, "nada sonando")
        comprobar(datos["state"]["queue_length"] == 0, "cola vacía")

    print("\n" + ("=" * 62))
    if fallos:
        print(f"❌ {len(fallos)} comprobación(es) fallaron:")
        for f in fallos:
            print(f"   - {f}")
        return 1
    print("✅ Prueba de humo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
