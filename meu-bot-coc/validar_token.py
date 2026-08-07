# -*- coding: utf-8 -*-
"""Valida o token da API do Clash of Clans direto contra a API.

Uso:
    python validar_token.py "<token colado do portal developer.clashofclans.com>"
    python validar_token.py            (lê o token do .env atual)

O script remove espacos/quebras de linha/aspa, confere se o payload e um JWT
JSON valido, tenta corrigir automaticamente corrupcoes simples de copia/colar
(caracter trocado na base64) e testa o token ao vivo na API.
"""
import sys
import re
import json
import base64
import requests
from dotenv import load_dotenv
import os

ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
CLAN_TAG = "%23LJCYRYGP"


def limpar(t):
    t = t.replace('"', "").replace("'", "")
    t = re.sub(r"\s+", "", t)
    if t.count(".") >= 2:
        partes = t.split(".")
        partes = partes[:2] + [".".join(partes[2:])]
        partes[0] = partes[0][: len(partes[0]) - len(partes[0]) % 4]
        sig = partes[2]
        sig = re.sub(r"[^A-Za-z0-9_\-]", "", sig)
        sig = sig[:86]
        partes[2] = sig
        t = ".".join(partes)
    return t


def decodificar_payload(token):
    try:
        partes = token.split(".")
        if len(partes) != 3:
            return None, "token nao tem 3 partes (header.payload.signature)"
        p = partes[1]
        raw = base64.urlsafe_b64decode(p + "=" * ((-len(p)) % 4))
        return json.loads(raw), None
    except Exception as e:
        return None, str(e)


def corrigir_payload(token):
    """Remove espacos e tenta corrigir um unico caractere corrompido na base64."""
    partes = token.split(".")
    p = partes[1]

    def decodifica_ok(segmento):
        try:
            raw = base64.urlsafe_b64decode(segmento + "=" * ((-len(segmento)) % 4))
            json.loads(raw)
            return True
        except Exception:
            return False

    for i in range(len(p)):
        for ch in ALPHA:
            cand = p[:i] + ch + p[i + 1:]
            if decodifica_ok(cand):
                return partes[0] + "." + cand + "." + partes[2]
    return None


def testar_api(token):
    r = requests.get(
        f"https://api.clashofclans.com/v1/clans/{CLAN_TAG}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    return r


def main():
    if len(sys.argv) > 1:
        token = limpar(sys.argv[1])
        origem = "argumento"
    else:
        load_dotenv()
        token = limpar(os.getenv("COC_TOKEN", ""))
        origem = ".env"
        if not token:
            print("Nenhum token informado e COC_TOKEN vazio no .env.")
            return

    print(f"Token ({origem}): {len(token)} caracteres")
    if not token:
        print("Token vazio.")
        return

    payload, erro = decodificar_payload(token)
    if payload is None:
        print(f"PAYLOAD INVALIDO ({erro}). Tentando corrigir corrupcao de copia/colar...")
        corrigido = corrigir_payload(token)
        if corrigido:
            print("Correcao encontrada:")
            print(corrigido)
            token = corrigido
            payload, erro = decodificar_payload(token)
            if payload is None:
                print(f"Corrigido mas ainda invalido: {erro}")
                return
        else:
            print("Nao foi possivel corrigir automaticamente. Copie o token de novo do portal.")
            return

    print("Payload OK:")
    print(json.dumps(payload, ensure_ascii=False))
    jti = payload.get("jti", "?")
    iat = payload.get("iat", "?")
    sub = payload.get("sub", "?")
    cidrs = payload.get("limits", [{}])[-1].get("cidrs", [])
    print(f"  jti : {jti}")
    print(f"  iat : {iat}")
    print(f"  sub : {sub}")
    print(f"  IPs permitidos : {cidrs}")

    print("\nTestando ao vivo na API do CoC...")
    try:
        r = testar_api(token)
        print(f"HTTP {r.status_code}")
        if r.status_code == 200:
            d = r.json()
            print(f"OK! Clan: {d.get('name')} | Nivel: {d.get('clanLevel')} | Membros: {d.get('members')}/50")
            print("\nToken FUNCIONA. Pode colar no .env.")
        else:
            print("Corpo:", r.text[:300])
            if r.status_code == 403:
                print(">>> 403 accessDenied: token rejeitado pela API.")
                print("    Verifique se a chave ainda existe em developer.clashofclans.com e")
                print("    se o IP permitido la e 3.15.210.77. Se o token foi copiado de um")
                print("    chat/documento, a copia pode ter corrompido a assinatura.")
    except Exception as e:
        print("Erro ao consultar a API:", e)


if __name__ == "__main__":
    main()
