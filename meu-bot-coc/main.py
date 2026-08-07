import os
import time
import json
import base64
import tempfile
import unicodedata
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import threading

load_dotenv()


def _env_limpo(nome, default=None):
    valor = os.getenv(nome, default)
    if valor is None:
        return valor
    return valor.strip().strip('"').strip("'")


COC_TOKEN = _env_limpo("COC_TOKEN")
if COC_TOKEN:
    COC_TOKEN = "".join(COC_TOKEN.split())  # remove qualquer espaço/quebra de linha interna
EVOLUTION_URL = _env_limpo("EVOLUTION_URL")
EVOLUTION_API_KEY = _env_limpo("EVOLUTION_API_KEY")
INSTANCE_NAME = _env_limpo("INSTANCE_NAME")
CLAN_TAG = _env_limpo("CLAN_TAG")
PORT = int(_env_limpo("PORT", "5000"))

# --- Configuração do fluxo de vendas/onboarding ---
# Número pessoal do administrador do sistema (você). Formato esperado: JID
# completo do WhatsApp, ex: "5511999999999@s.whatsapp.net".
# Fallback fixo usado quando ADMIN_NUMERO_PESSOAL vier ausente, vazio ou
# mal formatado no .env.
FALLBACK_ADMIN_NUMERO_PESSOAL = "5511987586783@s.whatsapp.net"


def _obter_admin_numero_pessoal():
    """Recupera ADMIN_NUMERO_PESSOAL do .env via os.getenv (através de
    _env_limpo) e garante que nunca retorne vazio/nulo: se a variável não
    existir, estiver vazia ou não for um JID válido, usa o número fixo de
    fallback — assim a notificação de confirmação nunca deixa de ser
    enviada por causa de condição falsa."""
    valor = _env_limpo("ADMIN_NUMERO_PESSOAL")
    if not valor or "@" not in valor:
        return FALLBACK_ADMIN_NUMERO_PESSOAL
    return valor


ADMIN_NUMERO_PESSOAL = _obter_admin_numero_pessoal()
PIX_CHAVE = _env_limpo("PIX_CHAVE")


def get_group_jid():
    load_dotenv()
    valor = (os.getenv("GROUP_JID") or "").strip()
    valor = valor.strip('"').strip("'")
    return valor


GROUP_JID = get_group_jid()

# ==========================================
# LIMITES DO SISTEMA
# ==========================================
MAX_TAGS_POR_NUMERO = 5      # cada número de WhatsApp pode vincular até 5 tags
MAX_CLAS_POR_GRUPO = 5       # cada grupo pode ter até 5 clãs registrados

# ==========================================
# ARQUIVOS DE DADOS (persistência em JSON)
# ==========================================
# Diretório persistente dos bancos internos. No Docker aponta para o volume
# /app/data (ver Dockerfile): registros, clãs, vendas e status NÃO se perdem
# em recompilações ou atualizações de containers.
DATA_DIR = os.getenv("DATA_DIR", "data").strip() or "data"

ARQUIVO_REGISTROS = os.path.join(DATA_DIR, "membros_registrados.json")
ARQUIVO_ESTRELAS = os.path.join(DATA_DIR, "estrelas_guerra_mensal.json")
ARQUIVO_RAIDE_STATUS = os.path.join(DATA_DIR, "raide_status.json")
ARQUIVO_GRUPOS_CLAS = os.path.join(DATA_DIR, "grupos_clas.json")
ARQUIVO_VENDAS = os.path.join(DATA_DIR, "vendas_pendentes.json")      # chat privado do comprador -> estado do funil de vendas
ARQUIVO_ADMINS_GRUPO = os.path.join(DATA_DIR, "admins_grupo.json")    # chat_jid do grupo -> número do admin com exclusividade
ARQUIVO_ADMINS_PENDENTES = os.path.join(DATA_DIR, "admins_pendentes.json")  # tag do clã -> número do admin (assinou no privado, aguardando entrar no grupo)
ARQUIVO_JOGOS_CLA = os.path.join(DATA_DIR, "jogos_cla_status.json")   # tag do clã -> baseline/estado dos Jogos do Clã do mês
ARQUIVO_CWL_STATUS = os.path.join(DATA_DIR, "cwl_status.json")        # tag do clã -> estado da CWL (bônus da liga já avisado, etc.)
ARQUIVO_DOACOES = os.path.join(DATA_DIR, "doacoes_status.json")       # tag do clã -> doações acumuladas por temporada (atual + passada)
ARQUIVO_TROFEUS_STATUS = os.path.join(DATA_DIR, "trofeus_status.json")  # tag do clã -> último mês em que o relatório de troféus foi enviado
ARQUIVO_FLUXO_VINCULAR = os.path.join(DATA_DIR, "fluxo_vincular.json")   # chat_jid do grupo -> estado dos fluxos de desvínculo (!desvincularcla / !desvincularplay)


# ==========================================
# LOCK DE ARQUIVOS (evita corrupção em escrita concorrente)
# ==========================================
# O bot tem pelo menos duas threads mexendo nos mesmos arquivos JSON ao
# mesmo tempo: a thread do loop principal (guerra, raide, CWL...) e a(s)
# thread(s) do Flask atendendo comandos no webhook. Sem lock, duas escritas
# simultâneas no mesmo arquivo podem se intercalar e corromper o JSON
# (arquivo com conteúdo misturado, JSON inválido na próxima leitura).
_locks_por_arquivo = {}
_lock_dos_locks = threading.Lock()


def _lock_para(caminho):
    caminho_abs = os.path.abspath(caminho)
    with _lock_dos_locks:
        lock = _locks_por_arquivo.get(caminho_abs)
        if lock is None:
            lock = threading.RLock()
            _locks_por_arquivo[caminho_abs] = lock
        return lock


def _carregar_json(caminho, default):
    with _lock_para(caminho):
        if os.path.exists(caminho):
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Erro ao carregar {caminho}: {e}")
        return default


def _salvar_json(caminho, dados):
    pasta = os.path.dirname(caminho) or "."
    os.makedirs(pasta, exist_ok=True)
    with _lock_para(caminho):
        # Escrita atômica: grava num arquivo temporário no MESMO diretório
        # (garante que fique no mesmo filesystem) e só troca pelo arquivo
        # final com os.replace, que no Linux é atômico. Assim, mesmo se o
        # processo morrer no meio da escrita, o arquivo original nunca fica
        # pela metade / corrompido — ou é o antigo, ou é o novo completo.
        descritor_tmp, caminho_tmp = tempfile.mkstemp(
            prefix=os.path.basename(caminho) + ".", suffix=".tmp", dir=pasta
        )
        try:
            with os.fdopen(descritor_tmp, "w", encoding="utf-8") as f:
                json.dump(dados, f, ensure_ascii=False, indent=4)
            os.replace(caminho_tmp, caminho)
        except Exception:
            try:
                os.remove(caminho_tmp)
            except OSError:
                pass
            raise


_ARQUIVOS_DE_DADOS = (
    ARQUIVO_REGISTROS,
    ARQUIVO_ESTRELAS,
    ARQUIVO_RAIDE_STATUS,
    ARQUIVO_GRUPOS_CLAS,
    ARQUIVO_VENDAS,
    ARQUIVO_ADMINS_GRUPO,
    ARQUIVO_ADMINS_PENDENTES,
    ARQUIVO_JOGOS_CLA,
    ARQUIVO_CWL_STATUS,
    ARQUIVO_DOACOES,
    ARQUIVO_TROFEUS_STATUS,
    ARQUIVO_FLUXO_VINCULAR,
)


def _migrar_dados_legados():
    """Migra os JSONs que ficavam na raiz do projeto para o DATA_DIR (volume
    persistente), sem perder dados em recompilações/atualizações. Roda uma
    única vez na inicialização. Também migra quando o destino existe mas
    está vazio ({}) e a origem tem conteúdo — caso comum quando um arquivo
    vazio foi criado no DATA_DIR antes de rodar a migração."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        return
    diretorio_atual = os.path.dirname(os.path.abspath(__file__))
    for arquivo in _ARQUIVOS_DE_DADOS:
        caminho_antigo = os.path.join(diretorio_atual, os.path.basename(arquivo))
        if not os.path.exists(caminho_antigo):
            continue
        destino_existe = os.path.exists(arquivo)
        destino_vazio = False
        if destino_existe:
            try:
                with open(arquivo, "r", encoding="utf-8") as f:
                    destino_vazio = (f.read().strip() in ("", "{}", "[]", "null"))
            except Exception:
                destino_vazio = False
        if not destino_existe or destino_vazio:
            try:
                import shutil
                shutil.move(caminho_antigo, arquivo)
                print(f"Dados migrados: {os.path.basename(arquivo)} -> {arquivo}")
            except Exception as e:
                print(f"Falha ao migrar {caminho_antigo}: {e}")


_migrar_dados_legados()


def carregar_registros():
    return _carregar_json(ARQUIVO_REGISTROS, {})


def salvar_registros(registros):
    _salvar_json(ARQUIVO_REGISTROS, registros)


def carregar_estatisticas():
    return _carregar_json(ARQUIVO_ESTRELAS, {})


def salvar_estatisticas(dados):
    _salvar_json(ARQUIVO_ESTRELAS, dados)


def carregar_raide_status():
    return _carregar_json(ARQUIVO_RAIDE_STATUS, {})


def salvar_raide_status(dados):
    _salvar_json(ARQUIVO_RAIDE_STATUS, dados)


def carregar_grupos_clas():
    return _carregar_json(ARQUIVO_GRUPOS_CLAS, {})


def salvar_grupos_clas(dados):
    _salvar_json(ARQUIVO_GRUPOS_CLAS, dados)


def carregar_vendas():
    return _carregar_json(ARQUIVO_VENDAS, {})


def salvar_vendas(dados):
    _salvar_json(ARQUIVO_VENDAS, dados)


def carregar_admins_grupo():
    return _carregar_json(ARQUIVO_ADMINS_GRUPO, {})


def salvar_admins_grupo(dados):
    _salvar_json(ARQUIVO_ADMINS_GRUPO, dados)


def obter_grupo_do_admin(numero_jid):
    """Dado o JID de um número, retorna o chat_jid do grupo onde ele é o
    administrador cadastrado com exclusividade, ou None se não for admin
    de nenhum grupo."""
    admins = carregar_admins_grupo()
    for grupo_jid, numero_admin in admins.items():
        if numero_admin == numero_jid:
            return grupo_jid
    return None


def carregar_admins_pendentes():
    return _carregar_json(ARQUIVO_ADMINS_PENDENTES, {})


def salvar_admins_pendentes(dados):
    _salvar_json(ARQUIVO_ADMINS_PENDENTES, dados)


def carregar_fluxo_vincular():
    return _carregar_json(ARQUIVO_FLUXO_VINCULAR, {})


def salvar_fluxo_vincular(dados):
    _salvar_json(ARQUIVO_FLUXO_VINCULAR, dados)


def registrar_admin_pendente(tag_clan, numero_admin_jid):
    """Chamado ao final do funil de vendas no privado: guarda o número do
    cliente como 'futuro admin' daquela tag de clã. Como nesse momento o
    bot ainda não está no grupo (só recebemos o link), não sabemos ainda
    o chat_jid real — por isso a chave é a TAG DO CLÃ, não o grupo."""
    if not tag_clan or not numero_admin_jid:
        return
    pendentes = carregar_admins_pendentes()
    pendentes[tag_clan] = numero_admin_jid
    salvar_admins_pendentes(pendentes)


def promover_admin_pendente_se_houver(tag_clan, chat_jid):
    """Chamado quando um clã é registrado num grupo (!vincularcla). Se
    havia um número pendente de vincular (de alguém que assinou o serviço
    no privado) para esta tag, ele passa a ser reconhecido automaticamente
    como o administrador cadastrado DESSE grupo — sem precisar de nenhum
    comando manual. Retorna o número promovido (ou None)."""
    pendentes = carregar_admins_pendentes()
    numero_admin_jid = pendentes.pop(tag_clan, None)
    if not numero_admin_jid:
        return None
    salvar_admins_pendentes(pendentes)
    admins = carregar_admins_grupo()
    admins[chat_jid] = numero_admin_jid
    salvar_admins_grupo(admins)
    return numero_admin_jid


# ==========================================
# RECONHECIMENTO DE ADMIN (SEM COMANDO MANUAL)
# ==========================================
# Quem pode usar comandos administrativos DENTRO DE UM GRUPO:
#   1) o dono do bot (ADMIN_NUMERO_PESSOAL), em qualquer grupo;
#   2) quem é admin/superadmin REAL do grupo agora no WhatsApp (checado ao
#      vivo na Evolution API, cargo de verdade — não precisa cadastro); ou
#   3) o número que assinou o serviço no chat privado e foi promovido
#      automaticamente (ver promover_admin_pendente_se_houver) ao número
#      cadastrado daquele grupo em admins_grupo.json.
# Nada disso depende de um comando manual tipo "!registraadm".
_CACHE_ADMINS_REAIS_GRUPO = {}   # chat_jid -> (timestamp, set(jids admin/superadmin))
_CACHE_ADMINS_REAIS_TTL_SEGUNDOS = 300  # 5 min, pra não martelar a Evolution API a cada comando


def _extrair_participantes_grupo(dados_resposta):
    """A Evolution API pode devolver os participantes em formatos levemente
    diferentes dependendo da versão/endpoint. Tenta os formatos mais comuns."""
    if not isinstance(dados_resposta, dict):
        return []
    if isinstance(dados_resposta.get("participants"), list):
        return dados_resposta["participants"]
    dados_aninhados = dados_resposta.get("data")
    if isinstance(dados_aninhados, dict) and isinstance(dados_aninhados.get("participants"), list):
        return dados_aninhados["participants"]
    if isinstance(dados_aninhados, list):
        return dados_aninhados
    return []


def obter_admins_reais_do_grupo(chat_jid):
    """Consulta a Evolution API e retorna o conjunto de JIDs que são
    admin/superadmin DE VERDADE do grupo agora no WhatsApp. Usa cache de
    alguns minutos (não é crítico ter 100% tempo real; evita sobrecarregar
    a API a cada comando digitado no grupo).

    ATENÇÃO: o endpoint/formato abaixo é o mais comum da Evolution API
    (findGroupInfos), mas pode variar conforme a versão instalada. Se o
    seu retorno vier vazio mesmo com o grupo tendo admins, confira no
    Swagger/documentação da sua instância qual é o endpoint correto de
    "buscar participantes do grupo" e ajuste a URL abaixo."""
    if not chat_jid or not str(chat_jid).endswith("@g.us"):
        return set()
    if not EVOLUTION_URL or not INSTANCE_NAME:
        return set()

    agora = time.time()
    em_cache = _CACHE_ADMINS_REAIS_GRUPO.get(chat_jid)
    if em_cache and (agora - em_cache[0]) < _CACHE_ADMINS_REAIS_TTL_SEGUNDOS:
        return em_cache[1]

    admins = set()
    url = f"{EVOLUTION_URL}/group/findGroupInfos/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    try:
        resposta = requests.get(url, headers=headers, params={"groupJid": chat_jid}, timeout=10)
        if resposta.ok:
            for participante in _extrair_participantes_grupo(resposta.json()):
                jid = participante.get("id") or participante.get("jid")
                cargo = (participante.get("admin") or "").lower()
                if jid and cargo in ("admin", "superadmin"):
                    admins.add(jid)
        else:
            print(f"Erro ao buscar admins do grupo {chat_jid}: status {resposta.status_code} - {resposta.text[:300]}")
    except Exception as e:
        print(f"Erro ao buscar admins do grupo {chat_jid}: {e}")

    # Guarda no cache mesmo se vier vazio (evita martelar a API em caso de
    # erro persistente); o próximo comando após o TTL tenta de novo.
    _CACHE_ADMINS_REAIS_GRUPO[chat_jid] = (agora, admins)
    return admins


def usuario_pode_administrar_grupo(chat_jid, remetente_jid):
    """True se remetente_jid pode executar comandos administrativos DENTRO
    do grupo chat_jid agora (dono do bot, admin real do WhatsApp, ou o
    admin cadastrado/promovido automaticamente para este grupo)."""
    if not remetente_jid:
        return False
    if ADMIN_NUMERO_PESSOAL and remetente_jid == ADMIN_NUMERO_PESSOAL:
        return True
    if not chat_jid or not str(chat_jid).endswith("@g.us"):
        return False
    if remetente_jid in obter_admins_reais_do_grupo(chat_jid):
        return True
    admins_cadastrados = carregar_admins_grupo()
    return admins_cadastrados.get(chat_jid) == remetente_jid


TEXTO_BLOQUEIO_ADMIN = "⛔ Esse comando é exclusivo do administrador do grupo (ou do dono do bot)."


def carregar_jogos_cla_status():
    return _carregar_json(ARQUIVO_JOGOS_CLA, {})


def salvar_jogos_cla_status(dados):
    _salvar_json(ARQUIVO_JOGOS_CLA, dados)


def carregar_cwl_status():
    return _carregar_json(ARQUIVO_CWL_STATUS, {})


def salvar_cwl_status(dados):
    _salvar_json(ARQUIVO_CWL_STATUS, dados)


def carregar_doacoes():
    return _carregar_json(ARQUIVO_DOACOES, {})


def salvar_doacoes(dados):
    _salvar_json(ARQUIVO_DOACOES, dados)


def carregar_trofeus_status():
    return _carregar_json(ARQUIVO_TROFEUS_STATUS, {})


def salvar_trofeus_status(dados):
    _salvar_json(ARQUIVO_TROFEUS_STATUS, dados)


# ==========================================
# TRADUÇÕES PARA PORTUGUÊS
# ==========================================
CARGOS_PT = {
    "leader": "Líder",
    "coLeader": "Co-líder",
    "admin": "Ancião",   # a API do CoC chama o Ancião de "admin"
    "elder": "Ancião",
    "member": "Membro",
}


def traduzir_cargo(role):
    return CARGOS_PT.get(role, "Membro" if not role else role)


def _abreviar_cargo(role):
    """Abreviação curta do cargo para caber em uma linha na lista de membros."""
    abreviacoes = {
        "leader": "LDR",
        "coLeader": "CLD",
        "admin": "ANC",
        "elder": "ANC",
        "member": "MBR",
    }
    return abreviacoes.get(role, "MBR" if not role else role)


# Tradução aproximada dos nomes de liga (jogo usa "Liga <Nível> <Numeral Romano>")
MAPA_PALAVRAS_LIGA = {
    "Bronze": "Bronze",
    "Silver": "Prata",
    "Gold": "Ouro",
    "Crystal": "Cristal",
    "Master": "Mestre",
    "Champion": "Campeão",
    "Titan": "Titã",
    "Legend": "Lendária",
    "Wood": "Madeira",
    "Clay": "Argila",
    "Copper": "Cobre",
    "Iron": "Ferro",
    "Tin": "Estanho",
    "Brass": "Latão",
    "Steel": "Aço",
    "Platinum": "Platina",
    "Titanium": "Titânio",
    "Diamond": "Diamante",
    "Emerald": "Esmeralda",
}


def traduzir_liga(nome_liga):
    """Traduz nomes de liga da API (em inglês) para português.
    Como a API não expõe um campo já localizado, fazemos o melhor esforço
    reconhecendo o padrão '<Nível> League <Numeral>' e remontando como
    'Liga <Nível> <Numeral>'."""
    if not nome_liga or nome_liga.strip().lower() == "unranked":
        return "Sem liga"

    partes = nome_liga.split()
    if "League" in partes:
        idx = partes.index("League")
        nivel_palavras = partes[:idx]
        resto = partes[idx + 1:]
        nivel_pt = " ".join(MAPA_PALAVRAS_LIGA.get(p, p) for p in nivel_palavras)
        sufixo = " ".join(resto)
        return ("Liga " + nivel_pt + (f" {sufixo}" if sufixo else "")).strip()

    return " ".join(MAPA_PALAVRAS_LIGA.get(p, p) for p in partes)


_ROMANOS_TIER = {"I": 1, "II": 2, "III": 3}


def liga_cwl_curta(nome_liga):
    """Converte o nome da liga de guerra da CWL vindo da API em formato curto
    em português: 'Titan League I' -> 'Titã 1', 'Master League III' -> 'Mestre 3'.
    Devolve '—' quando vazio e 'Sem liga' para 'Unranked'."""
    if not nome_liga or not str(nome_liga).strip():
        return "—"
    nome_liga = str(nome_liga).strip()
    if nome_liga.lower() == "unranked":
        return "Sem liga"

    partes = [p for p in nome_liga.split() if p != "League"]
    if partes and partes[-1] in _ROMANOS_TIER:
        numero = _ROMANOS_TIER[partes[-1]]
        partes = partes[:-1]
    else:
        numero = None

    nivel_pt = " ".join(MAPA_PALAVRAS_LIGA.get(p, p) for p in partes)
    if numero:
        return f"{nivel_pt} {numero}"
    return nivel_pt


_MAPA_LIGA_TIER = {
    "skeleton": "Esqueleto",
    "barbarian": "Bárbaro",
    "archer": "Arqueiro",
    "wizard": "Mago",
    "valkyrie": "Valquíria",
    "witch": "Bruxa",
    "golem": "Golem",
    "p.e.k.k.a": "P.E.K.K.A",
    "titan": "Titã",
    "dragon": "Dragão",
    "electro": "Elétrica",
}

_ROMANOS_LENDA = {"I": "1", "II": "2", "III": "3"}


def traduzir_liga_tier(nome_tier):
    """Traduz o patamar RANQUEADO atual do jogador (campo 'leagueTier' da API,
    criado na reformulação de 2026) para português compacto:
    'Legend I' -> 'Lenda 1', 'Electro League 33' -> 'Elétrica 33',
    'Titan League 25' -> 'Titã 25', 'Unranked' -> 'Sem liga'."""
    if not nome_tier or not str(nome_tier).strip():
        return None
    nome = str(nome_tier).strip()
    baixo = nome.lower()

    if baixo == "unranked":
        return "Sem liga"

    if baixo.startswith("legend"):
        romano = baixo.split()[-1] if baixo.split() else ""
        numero = _ROMANOS_LENDA.get(romano.upper(), romano.upper())
        return f"Lenda {numero}".strip()

    partes = nome.split()
    for i, tok in enumerate(partes):
        if tok.lower() == "league":
            chave = " ".join(partes[:i])
            base = _MAPA_LIGA_TIER.get(chave.lower())
            if not base:
                return nome
            numero = partes[i + 1] if i + 1 < len(partes) else ""
            return f"{base} {numero}".strip()
    return nome


def liga_do_player(dados_player):
    """Liga ranqueada ATUAL do jogador: prioriza o novo campo 'leagueTier'
    (criado na reformulação de 2026, ex: 'Legend I', 'Electro League 33') e cai
    para o campo antigo 'league' quando o tier estiver ausente."""
    dados = dados_player or {}
    tier = (((dados.get("leagueTier") or {}) or {}).get("name")) or ""
    if tier:
        return traduzir_liga_tier(tier)
    return traduzir_liga(((dados.get("league") or {}) or {}).get("name"))


# ==========================================
# UTILITÁRIOS DE TAG / API
# ==========================================
def _saneia_tag(tag):
    """Normaliza os caracteres de uma tag vinda do usuário. O alfabeto de
    tag do Clash of Clans é restrito e NÃO contém a letra 'O' — apenas o
    dígito '0' (e também não tem 'I'/'1'/etc.). Tags digitadas ou copiadas
    com 'O' (ex: '#20C9CG0J' parecendo '#2OC9CG0J') são arrumadas para '0'
    antes de consultar a API, evitando falso 'tag não encontrada'."""
    tag = (tag or "").strip().upper()
    return tag.replace("O", "0")


def tag_para_url(tag):
    tag = _saneia_tag(tag)
    if not tag.startswith("#"):
        tag = "#" + tag.lstrip("#")
    return tag.replace("#", "%23")


def gerar_link_clan(tag):
    """Link oficial do Clash of Clans que abre o perfil do clã direto no jogo."""
    return f"https://link.clashofclans.com/en?action=OpenClanProfile&tag={tag_para_url(tag)}"


def normalizar_tag(tag):
    tag = _saneia_tag(tag)
    if not tag:
        return None
    if not tag.startswith("#"):
        tag = "#" + tag.lstrip("#")
    return tag


def normalizar_numero_para_jid(numero_bruto):
    """Recebe um número de telefone digitado pelo cliente (com ou sem
    formatação, com ou sem +, com ou sem @s.whatsapp.net) e retorna o JID
    padrão do WhatsApp, ex: '5511999999999@s.whatsapp.net'. Retorna None se
    não sobrar nenhum dígito."""
    if not numero_bruto:
        return None
    numero_bruto = numero_bruto.strip()
    if "@" in numero_bruto:
        return numero_bruto
    somente_digitos = "".join(c for c in numero_bruto if c.isdigit())
    if not somente_digitos:
        return None
    return f"{somente_digitos}@s.whatsapp.net"


# ==========================================
# CÁLCULO DA TEMPORADA (RESET DE TROFÉUS/DOAÇÕES)
# ==========================================
# A Supercell reseta troféus e doações na ÚLTIMA segunda-feira de cada mês,
# à meia-noite no horário do Leste dos EUA (America/New_York) — não no
# último dia do mês do calendário. Usamos isso (em vez de "virada de mês")
# para saber com precisão quando a temporada realmente terminou.
FUSO_RESET_TEMPORADA = ZoneInfo("America/New_York")
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def agora_brasilia():
    """Hora atual no fuso de Brasília (UTC-3), usada em todos os
    agendamentos do bot (avisos diários, virada de mês, etc.) — assim o
    comportamento não depende do fuso horário configurado no servidor."""
    return datetime.now(FUSO_BRASILIA)


def _ultima_segunda_do_mes(ano, mes):
    """Retorna a data (date) da última segunda-feira do mês/ano informado."""
    if mes == 12:
        primeiro_dia_prox_mes = date(ano + 1, 1, 1)
    else:
        primeiro_dia_prox_mes = date(ano, mes + 1, 1)
    ultimo_dia_mes = primeiro_dia_prox_mes - timedelta(days=1)
    dias_ate_segunda = (ultimo_dia_mes.weekday() - 0) % 7  # weekday(): segunda-feira = 0
    return ultimo_dia_mes - timedelta(days=dias_ate_segunda)


def obter_fim_temporada_atual(agora_utc=None):
    """Retorna o datetime (em UTC) do fim da temporada de troféus/doações
    que está rolando agora — a próxima última segunda-feira à meia-noite
    horário do Leste dos EUA."""
    agora_utc = agora_utc or datetime.now(timezone.utc)
    agora_et = agora_utc.astimezone(FUSO_RESET_TEMPORADA)

    candidato = _ultima_segunda_do_mes(agora_et.year, agora_et.month)
    fim_et = datetime(candidato.year, candidato.month, candidato.day, 0, 0, 0, tzinfo=FUSO_RESET_TEMPORADA)

    if agora_et >= fim_et:
        # a última segunda deste mês já passou; a próxima virada é no mês seguinte
        prox_mes = agora_et.month + 1
        prox_ano = agora_et.year
        if prox_mes > 12:
            prox_mes = 1
            prox_ano += 1
        candidato = _ultima_segunda_do_mes(prox_ano, prox_mes)
        fim_et = datetime(candidato.year, candidato.month, candidato.day, 0, 0, 0, tzinfo=FUSO_RESET_TEMPORADA)

    return fim_et.astimezone(timezone.utc)


def identificador_temporada_atual(agora_utc=None):
    """Identificador único (string, ex: '2026-08-31') da temporada em
    andamento, baseado na data do seu fim. Usado como chave nos arquivos de
    persistência de doações/troféus."""
    return obter_fim_temporada_atual(agora_utc).strftime("%Y-%m-%d")


def requisitar_coc(endpoint, timeout=10, tentativas=1):
    url = f"https://api.clashofclans.com/v1{endpoint}"
    headers = {
        "Authorization": f"Bearer {COC_TOKEN}",
        "Accept": "application/json"
    }
    for tentativa in range(tentativas):
        try:
            resposta = requests.get(url, headers=headers, timeout=timeout)
            if resposta.status_code == 200:
                return resposta.json()
            print(f"Erro ao consultar CoC API ({url}): status {resposta.status_code} - {resposta.text[:300]} | origem: {_nome_funcao_chamadora()}")
            return None
        except Exception as e:
            if tentativa < tentativas - 1:
                print(f"⚠️ CoC API não respondeu ({url}) — tentando de novo (tentativa {tentativa + 1}/{tentativas})...")
                time.sleep(2)
                continue
            print(f"Erro ao consultar CoC API ({url}): {e} | origem: {_nome_funcao_chamadora()}")
    return None


def _nome_funcao_chamadora():
    """Nome da função que chamou requisitar_coc — usado no log de erros para
    rastrear de onde vem cada consulta à API."""
    try:
        import sys
        frame = sys._getframe(2)
        nome = frame.f_code.co_name
        linha = frame.f_lineno
        return f"{nome} (linha {linha})"
    except Exception:
        return "desconhecida"


def requisitar_coc_com_status(endpoint):
    """Consulta a API do Clash of Clans e devolve a tupla (status, dados) —
    diferente de requisitar_coc, que funde tudo em None. Necessário para a
    VALIDAÇÃO RIGOROSA de tags no onboarding: somente aqui dá pra distinguir
    'tag não encontrada' (status 404) de 'a API está fora do ar ou com erro'
    (falha de conexão, 403/429/5xx...). Em falha de rede/timeout devolve
    (None, None); em qualquer erro do servidor devolve (status, dados)."""
    url = f"https://api.clashofclans.com/v1{endpoint}"
    headers = {
        "Authorization": f"Bearer {COC_TOKEN}",
        "Accept": "application/json"
    }
    try:
        resposta = requests.get(url, headers=headers, timeout=10)
        if resposta.status_code == 200:
            return resposta.status_code, resposta.json()
        print(f"Erro ao consultar CoC API ({url}): status {resposta.status_code} - {resposta.text[:300]} | origem: {_nome_funcao_chamadora()}")
        return resposta.status_code, None
    except Exception as e:
        print(f"Erro ao consultar CoC API ({url}): {e} | origem: {_nome_funcao_chamadora()}")
        return None, None


def validar_tag_no_onboarding(tag, tipo_esperado):
    """VALIDAÇÃO RIGOROSA de tag usada no onboarding do chat privado.
    Consulta a API de verdade (nunca aceita tag por confiança) e DISTINGUE
    corretamente o tipo da tag:
      tipo_esperado = "cla"    (campo 'tag do clã')
      tipo_esperado = "jogador" (campo 'tag do líder')

    IMPORTANTE: a API do Clash of Clans mantém tags de jogador e tags de clã
    no MESMO espaço de nomes, e elas podem COINCIDIR — a mesma sequência pode
    existir como jogador E como clã (ex: '#2YL8YRRVV' já foi observado com um
    jogador de verdade e um clã vazio de nível 1 usando exatamente a mesma
    tag). Por isso NUNCA confiamos no resultado de um endpoint para afirmar o
    tipo quando o outro tipo pode colidir: consultamos PRIMEIRO o endpoint
    do TIPO ESPERADO e só recorremos ao outro endpoint se o primeiro retornar
    404 — assim uma tag de jogador que colide com um clã vazio continua
    sendo aceita como jogador.

    Retorna uma tupla (resultado, dados):
      ("ok", dados)          — a tag é válida E é do tipo esperado.
      ("tipo_errado", dados) — a tag existe na API, mas é o outro tipo
                               (ex: o cliente digitou uma tag de jogador no
                               campo de clã) — retorna os dados do outro
                               tipo para a mensagem explicar direitinho.
      ("nao_existe", None)   — nenhum dos dois tipos existe (tag inventada).
      ("erro_api", None)     — a API não respondeu de forma conclusiva;
                               NÃO se pode afirmar que a tag é inválida."""
    tag_url = tag_para_url(tag)
    if tipo_esperado == "cla":
        endpoint_tipo, dados_tipo = requisitar_coc_com_status(f"/clans/{tag_url}")
        if endpoint_tipo != 200:
            endpoint_outro, dados_outro = requisitar_coc_com_status(f"/players/{tag_url}")
        else:
            endpoint_outro, dados_outro = None, None
    else:  # "jogador"
        endpoint_tipo, dados_tipo = requisitar_coc_com_status(f"/players/{tag_url}")
        if endpoint_tipo != 200:
            endpoint_outro, dados_outro = requisitar_coc_com_status(f"/clans/{tag_url}")
        else:
            endpoint_outro, dados_outro = None, None

    if endpoint_tipo == 200:
        return "ok", dados_tipo
    if endpoint_outro == 200:
        return "tipo_errado", dados_outro
    if endpoint_tipo is None or endpoint_outro is None:
        return "erro_api", None
    return "nao_existe", None


def enviar_whatsapp(texto, numero=None, mencionar_todos=False, mencionados=None):
    destino = (numero or GROUP_JID or get_group_jid()).strip()
    if not destino:
        return False

    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "number": destino,
        "text": texto
    }
    if mencionar_todos:
        payload["mentionsEveryOne"] = True
    if mencionados:
        payload["mentioned"] = mencionados
    try:
        resposta = requests.post(url, json=payload, headers=headers, timeout=10)
        if not resposta.ok:
            print(f"Erro ao enviar WhatsApp para {destino}: status {resposta.status_code} - {resposta.text}")
            return False
        return True
    except Exception as e:
        print(f"Erro ao enviar WhatsApp para {destino}: {e}")
        return False


def enviar_imagem_whatsapp(caminho_imagem, numero=None, legenda=""):
    """Envia uma imagem (PNG) via Evolution API (endpoint sendMedia). Usado
    pelo !perfilcla para mandar o card do clã. Retorna True em caso de
    sucesso."""
    destino = (numero or GROUP_JID or get_group_jid()).strip()
    if not destino or not os.path.exists(caminho_imagem):
        return False
    try:
        with open(caminho_imagem, "rb") as arquivo:
            imagem_b64 = base64.b64encode(arquivo.read()).decode("utf-8")
    except Exception as e:
        print(f"Erro ao ler imagem para envio: {e}")
        return False

    url = f"{EVOLUTION_URL}/message/sendMedia/{INSTANCE_NAME}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "number": destino,
        "mediatype": "image",
        "media": imagem_b64,
        "caption": legenda,
        "fileName": "perfil_cla.png",
        "mimetype": "image/png"
    }
    try:
        resposta = requests.post(url, json=payload, headers=headers, timeout=20)
        if not resposta.ok:
            print(f"Erro ao enviar imagem WhatsApp para {destino}: status {resposta.status_code} - {resposta.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"Erro ao enviar imagem WhatsApp para {destino}: {e}")
        return False


def buscar_nome_contato(jid):
    """Tenta obter o nome do contato salvo no WhatsApp via Evolution API.
    IMPORTANTE / LIMITAÇÃO CONHECIDA: a Evolution API (endpoint
    /chat/fetchProfile) só retorna o "pushName" (nome que a própria pessoa
    definiu no WhatsApp), não o nome que VOCÊ salvou na agenda do seu
    celular — isso não é exposto pela API do WhatsApp/Baileys. Por isso,
    se a pessoa não tiver definido um nome de perfil, ou se a consulta
    falhar, caímos de volta para o número."""
    if not jid or not EVOLUTION_URL or not INSTANCE_NAME:
        return None
    numero = jid.split("@")[0]
    url = f"{EVOLUTION_URL}/chat/fetchProfile/{INSTANCE_NAME}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        resposta = requests.post(url, json={"number": numero}, headers=headers, timeout=10)
        if resposta.ok:
            dados = resposta.json()
            if isinstance(dados, dict):
                nome = dados.get("name") or dados.get("pushName")
                if nome:
                    return nome
    except Exception as e:
        print(f"Erro ao buscar nome de contato ({jid}): {e}")
    return None


def formatar_status_ataques(nome, ataques, max_ataques=2):
    """Monta a linha de status de ataques no padrão visual definido:
    ✅/🟡/🔴 [Nick] (n/max) atk: 1° X⭐- Y% - 2° X⭐- Y% ..."""
    ataques = ataques or []
    qtd = len(ataques)
    if qtd >= max_ataques and max_ataques > 0:
        emoji = "✅"
    elif qtd > 0:
        emoji = "🟡"
    else:
        emoji = "🔴"

    partes = []
    for i in range(max_ataques):
        if i < qtd:
            a = ataques[i]
            estrelas = a.get("stars", 0)
            destruicao = a.get("destructionPercentage", 0) or 0
            partes.append(f"{i + 1}° {estrelas}⭐- {destruicao:.0f}%")
        else:
            partes.append(f"{i + 1}° 0⭐- 0%")

    return f"{emoji} {nome} ({qtd}/{max_ataques}) atk: " + " - ".join(partes)


def formatar_status_ataques_com_mencao(nome, ataques, max_ataques=2, jid_vinculado=None):
    """Igual a formatar_status_ataques, mas troca o nick por @número quando o
    jogador tem WhatsApp vinculado E ainda não completou os ataques. Quem já
    atacou ou não tem vínculo aparece normalmente só com o nick."""
    ataques = ataques or []
    qtd = len(ataques)
    if qtd >= max_ataques and max_ataques > 0:
        emoji = "✅"
    elif qtd > 0:
        emoji = "🟡"
    else:
        emoji = "🔴"

    partes = []
    for i in range(max_ataques):
        if i < qtd:
            a = ataques[i]
            estrelas = a.get("stars", 0)
            destruicao = a.get("destructionPercentage", 0) or 0
            partes.append(f"{i + 1}° {estrelas}⭐- {destruicao:.0f}%")
        else:
            partes.append(f"{i + 1}° 0⭐- 0%")

    completou = qtd >= max_ataques and max_ataques > 0
    identificacao = f"@{jid_vinculado.split('@')[0]}" if (jid_vinculado and not completou) else nome

    return f"{emoji} {identificacao} ({qtd}/{max_ataques}) atk: " + " - ".join(partes)


# ==========================================
# REGISTROS / VÍNCULO WHATSAPP <-> JOGADOR
# ==========================================
def contar_tags_do_numero(registros, jid, ignorar_tag=None):
    total = 0
    for tag, info in registros.items():
        if ignorar_tag and tag == ignorar_tag:
            continue
        if info.get("whatsapp_jid") == jid:
            total += 1
    return total


# ==========================================
# CLÃS REGISTRADOS POR GRUPO
# ==========================================
def obter_clas_do_grupo(chat_jid):
    grupos = carregar_grupos_clas()
    return grupos.get(chat_jid, {})


def grupo_esta_registrado(chat_jid):
    """Retorna True se o grupo já tiver ao menos um clã cadastrado (via
    !vincularcla). Grupos cadastrados respondem apenas aos comandos; grupos
    sem cadastro recebem a resposta automática de vendas."""
    return bool(obter_clas_do_grupo(chat_jid))


def guerra_ligada_para_clan(chat_jid, tag):
    clas = obter_clas_do_grupo(chat_jid)
    info = clas.get(tag)
    if info is None:
        return True  # clã não gerenciado explicitamente = comportamento padrão (ligado)
    return info.get("guerra_on", True)


def avisos_auto_ligados(chat_jid, tag):
    """Gate dos envios automáticos de GUERRA/CWL (bônus, troféus, estrelas).
    Ao vincular um clã o padrão é DESLIGADO: nada de guerra/CWL sai sozinho
    até o admin ativar (!avisosguerraon). Raide da Capital e
    Jogos do Clã NÃO passam por aqui — são sempre automáticos."""
    clas = obter_clas_do_grupo(chat_jid)
    info = clas.get(tag)
    if info is None:
        return True
    return info.get("avisos_auto", False)


def cwl_detalhado_ligado(chat_jid, tag):
    """Estado do aviso periódico (4 em 4 horas) da CWL: True = manda lista
    detalhada marcando vinculados; False = manda só a frase padrão, sem
    nomes."""
    clas = obter_clas_do_grupo(chat_jid)
    info = clas.get(tag)
    if info is None:
        return True
    return info.get("aviso_cwl_detalhado", True)


def obter_pares_grupo_cla():
    """Retorna a lista de pares (grupo, tag_do_cla) que o bot deve monitorar
    automaticamente. Sempre inclui os clãs registrados via !vincularcla, e,
    para manter compatibilidade com a configuração antiga (um único clã),
    também inclui o par (GROUP_JID, CLAN_TAG) do .env caso nenhum grupo
    tenha clãs registrados ainda."""
    grupos = carregar_grupos_clas()
    pares = []
    for grupo_jid, clas in grupos.items():
        for tag in clas.keys():
            pares.append((grupo_jid, tag))
    if not pares and GROUP_JID and CLAN_TAG:
        pares.append((GROUP_JID, normalizar_tag(CLAN_TAG)))
    return pares


def inicializar_grupo_padrao():
    """Na primeira execução, se ainda não existir nenhum grupo/clã
    registrado e as variáveis de ambiente legadas (GROUP_JID/CLAN_TAG)
    estiverem definidas, registra esse clã automaticamente no grupo
    padrão para que !avisosguerraon/!avisosguerraoff funcionem desde o início."""
    grupos = carregar_grupos_clas()
    if grupos:
        return
    if GROUP_JID and CLAN_TAG:
        grupos[GROUP_JID] = {normalizar_tag(CLAN_TAG): {"guerra_on": True}}
        salvar_grupos_clas(grupos)


# ==========================================
# 1. CAPITAL DO CLÃ (RAIDES)
# ==========================================
def relatorio_raides(temporada, grupo=None, nome_clan=None):
    """Relatório FINAL da raide da Capital: lista os membros com tag vinculada
    (registros do bot) com a quantidade de ATAQUES e de OURO de cada um — as
    únicas informações de desempenho saem aqui; os lembretes diários mostram
    apenas o nick/quem ainda não atacou (sem ataques e sem ouro)."""
    membros = temporada.get("members", [])
    registros = carregar_registros() if membros else {}

    vinculados = [m for m in membros if m.get("tag") and registros.get(m.get("tag"))]
    if not vinculados:
        return

    def fmt_num(v):
        return f"{int(v or 0):,}".replace(",", ".")

    linhas = []
    total_ouro = 0
    for m in vinculados:
        ataques = m.get("attacks") or 0
        ouro = m.get("capitalResourcesLooted") or 0
        total_ouro += ouro
        linhas.append(f"• {m.get('name')} — {fmt_num(ataques)} ataque(s) — 🪙 {fmt_num(ouro)}")

    texto = "🏰 *RELATÓRIO DA CAPITAL (RAIDES)* 🏰\n\n"
    if nome_clan:
        texto += f"🏛️ Clã: *{nome_clan}*\n\n"
    texto += "🧑‍🚀 *Jogadores com tag vinculada:*\n" + "\n".join(linhas)
    texto += f"\n\n💰 Total de ouro (vinculados): {fmt_num(total_ouro)}"
    enviar_whatsapp(texto, grupo)


def loop_raides(grupo, tag):
    # Raide da Capital é SEMPRE automática (relatório final + lembretes),
    # sem depender de !avisosguerraon/off.
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/capitalraidseasons")
    if not dados or "items" not in dados or len(dados["items"]) == 0:
        return

    temporada = dados["items"][0]
    estado = temporada.get("state")  # "ended" quando o fim de semana termina

    status_geral = carregar_raide_status()
    status = status_geral.setdefault(tag, {})
    alterou = False

    # Relatório apenas no FINAL do evento (raide terminou), sem aviso de
    # início e sem lembrete diário.
    fim_temporada = temporada.get("endTime")
    if estado == "ended" and fim_temporada:
        if status.get("ultimo_enviado") != fim_temporada:
            relatorio_raides(temporada, grupo, _nome_oficial_clan(tag, grupo))
            status["ultimo_enviado"] = fim_temporada
            alterou = True

    if alterou:
        status_geral[tag] = status
        salvar_raide_status(status_geral)


def loop_aviso_raide_diario(grupo, tags):
    """Lembretes da raide da Capital enquanto houver raide em andamento no
    grupo (state == 'ongoing'):
      • sexta e sábado: 1 aviso por dia, às 12h;
      • domingo: 1 aviso de 6 em 6 horas (0h, 6h, 12h e 18h).
    Marca (@número) TODOS que têm tag vinculada E ainda não atacaram; quem já
    atacou (mesmo com vínculo) e quem não tem vínculo aparecem só com o nick.
    Se o grupo tem mais de um clã, os pendentes de TODOS saem em UMA lista só
    (sem identificar de qual clã). É sempre automático: não depende de
    !avisosguerraon/off."""
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return

    agora = agora_brasilia()
    hoje = str(agora.date())
    dia_semana = agora.weekday()  # 0=segunda ... 4=sexta, 5=sábado, 6=domingo

    # Hora correta conforme o dia da semana
    na_hora_12h = agora.hour == 12 and agora.minute == 0
    slot_domingo = dia_semana == 6 and agora.minute == 0 and agora.hour % 6 == 0
    if not (na_hora_12h or slot_domingo):
        return

    # Junta as temporadas de raide EM ANDAMENTO de todos os clãs do grupo
    temporadas = []
    for tag in tags:
        dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/capitalraidseasons")
        if not dados or "items" not in dados or len(dados["items"]) == 0:
            continue
        temporada = dados["items"][0]
        if temporada.get("state") == "ongoing":
            temporadas.append(temporada)
    if not temporadas:
        return

    # Dedup POR GRUPO: um único lembrete por grupo (e não por clã), para o
    # grupo com vários clãs não receber uma mensagem para cada clã.
    slot_chave = f"{hoje} 12h" if na_hora_12h else f"{hoje} {agora.hour}h"
    status_geral = carregar_raide_status()
    status_grupo = status_geral.setdefault(grupo, {})
    if status_grupo.get("ultimo_lembrete") == slot_chave:
        return

    registros = carregar_registros()
    linhas = []
    jids_mencionados = []
    vistos = set()
    for temporada in temporadas:
        for m in temporada.get("members", []):
            if not m.get("tag") or m["tag"] in vistos:
                continue
            vistos.add(m["tag"])
            nome = m.get("name", "Desconhecido")
            reg = registros.get(m["tag"])
            jid = reg.get("whatsapp_jid") if reg else None
            if jid and (m.get("attacks") or 0) == 0:
                linhas.append(f"🔴 {nome}")
                if jid not in jids_mencionados:
                    jids_mencionados.append(jid)
            else:
                linhas.append(f"✅ {nome}")

    if not linhas:
        return

    nome_clan_linha = ""
    if len(tags) == 1:
        nome_clan_linha = f"🏛️ Clã: *{_nome_oficial_clan(tags[0], grupo)}*\n\n"
    texto = (
        "🏰 *RAIDE DA CAPITAL EM ANDAMENTO!* 🏰\n\n"
        f"{nome_clan_linha}"
        "Não esqueçam de atacar hoje! ⚔️\n\n"
        "🔴 = ainda não atacou\n"
        "✅ = já atacou\n\n"
        + "\n".join(linhas)
    )
    if jids_mencionados:
        enviar_whatsapp(texto, grupo, mencionados=jids_mencionados)
    else:
        enviar_whatsapp(texto, grupo)
    status_grupo["ultimo_lembrete"] = slot_chave
    status_geral[grupo] = status_grupo
    salvar_raide_status(status_geral)


# ==========================================
# 2. JOGOS DO CLÃ
# ==========================================
def loop_jogos_do_cla(grupo, tags):
    """Durante a janela dos Jogos do Clã (dias 22 a 28), manda TODO dia ao
    meio-dia um LEMBRETE pequeno para o grupo — SEM marcar ninguém — até o
    término do evento. Se o grupo tem mais de um clã, sai UMA mensagem só.
    É sempre automático: não depende de !avisosguerraon/off."""
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return

    agora = agora_brasilia()
    if not (22 <= agora.day <= 28):
        return
    if not (agora.hour == 12 and agora.minute == 0):
        return

    hoje = str(agora.date())
    status_geral = carregar_jogos_cla_status()
    status_grupo = status_geral.setdefault(grupo, {})
    if status_grupo.get("aviso_dia") == hoje:
        return

    texto = "🎯 *JOGOS DO CLÃ EM ANDAMENTO!* 🎯\n\n"
    if len(tags) == 1:
        texto += f"🏛️ Clã: *{_nome_oficial_clan(tags[0], grupo)}*\n\n"
    texto += "Não esqueçam de jogar hoje e garantir a pontuação máxima para o clã! ⚔️"

    enviar_whatsapp(texto, grupo)
    status_grupo["aviso_dia"] = hoje
    status_geral[grupo] = status_grupo
    salvar_jogos_cla_status(status_geral)


# ==========================================
# 3. GUERRA DE CLÃS
# ==========================================
def relatorio_guerra(tag, marcar_vinculados=True):
    """Retorna (texto, tags_pendentes, jids, fingerprint) do status atual da
    guerra do clã informado, ou (None, [], [], None) se não houver guerra em
    andamento. Com marcar_vinculados (padrão), quem tem vínculo e ainda não
    atacou aparece marcado (@número), e quem NÃO tem vínculo (ou já completou)
    aparece só com o nick. O fingerprint (estado dos ataques) permite que o
    loop detecte se houve qualquer alteração desde o último envio."""
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/currentwar")
    if not dados:
        return None, [], [], None

    estado = dados.get("state")
    if estado != "inWar":
        return None, [], [], None

    max_ataques = dados.get("attacksPerMember", 2)
    nome_cla = dados.get("clan", {})
    inimigo_cla = dados.get("opponent", {})

    nome_nosso_cla = nome_cla.get("name", "Nosso Clã")
    nome_inimigo = inimigo_cla.get("name", "Inimigo")

    estrelas_nosso = nome_cla.get("stars", 0)
    estrelas_inimigo = inimigo_cla.get("stars", 0)

    membros = nome_cla.get("members", [])

    jids_por_tag = {}
    if marcar_vinculados:
        registros = carregar_registros()
        for m in membros:
            reg = registros.get(m.get("tag"))
            if reg and reg.get("whatsapp_jid"):
                jids_por_tag[m.get("tag")] = reg["whatsapp_jid"]

    linhas = []
    pendentes = []
    jids = []
    for m in membros:
        ataques = m.get("attacks", []) or []
        jid_vinculado = jids_por_tag.get(m.get("tag"))
        if marcar_vinculados:
            linhas.append(formatar_status_ataques_com_mencao(
                m.get("name"), ataques, max_ataques, jid_vinculado
            ))
        else:
            linhas.append(formatar_status_ataques(m.get("name"), ataques, max_ataques))
        if len(ataques) < max_ataques:
            pendentes.append(m.get("tag"))
            if jid_vinculado and jid_vinculado not in jids:
                jids.append(jid_vinculado)

    texto = (
        f"⚔️ *STATUS DA GUERRA DE CLÃS* ⚔️\n\n"
        f"🛡️ *{nome_nosso_cla}*  ⭐ {estrelas_nosso}\n"
        f"🏴‍☠️ *{nome_inimigo}*  ⭐ {estrelas_inimigo}\n\n"
        f"📋 *Membros:*\n" + "\n".join(linhas)
    )

    fingerprint = tuple(sorted(
        (m.get("tag") or "", len(m.get("attacks", []) or [])) for m in membros
    ))

    return texto, pendentes, jids, fingerprint


_alertas_guerra = {}  # tag -> {"inicio": bool, "fim": bool}


def loop_guerra(grupo, tag):
    if not avisos_auto_ligados(grupo, tag):
        return
    estado_alertas = _alertas_guerra.setdefault(tag, {"inicio": False, "fim": False})

    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/currentwar")
    if not dados or "state" not in dados:
        return

    estado = dados.get("state")
    agora = datetime.utcnow()

    if estado == "notInWar":
        estado_alertas["inicio"] = False
        estado_alertas["fim"] = False
        return

    if estado == "preparation":
        start_str = dados.get("startTime")
        if start_str and not estado_alertas["inicio"]:
            try:
                start_time = datetime.strptime(start_str.split(".")[0], "%Y%m%dT%H%M%S")
                falta = start_time - agora
                if timedelta(minutes=59) <= falta <= timedelta(minutes=61):
                    inimigo = dados.get("opponent", {}).get("name", "Desconhecido")
                    msg = f"⚠️ *ATENÇÃO CLÃ!* A Guerra de *{_nome_oficial_clan(tag, grupo)}* contra *{inimigo}* vai começar em *1 hora*!"
                    enviar_whatsapp(msg, grupo)
                    estado_alertas["inicio"] = True
            except Exception:
                pass

    elif estado == "inWar":
        end_str = dados.get("endTime")
        if end_str and not estado_alertas["fim"]:
            try:
                end_time = datetime.strptime(end_str.split(".")[0], "%Y%m%dT%H%M%S")
                falta = end_time - agora
                if timedelta(minutes=59) <= falta <= timedelta(minutes=61):
                    msg = f"🚨 *ATENÇÃO CLÃ!* A Guerra de *{_nome_oficial_clan(tag, grupo)}* está acabando! Faltam apenas *1 hora* para o fim, corram para atacar!"
                    enviar_whatsapp(msg, grupo)
                    estado_alertas["fim"] = True
            except Exception:
                pass



# ==========================================
# 3B. CWL (LIGA DE GUERRA DE CLÃS)
# ==========================================
def obter_guerra_cwl_atual(tag_clan):
    """Busca a rodada da CWL atualmente em 'preparation' ou 'inWar' para o
    clã informado. Retorna um dict {'guerra': ..., 'nosso': ..., 'inimigo': ...}
    ou None se o clã não estiver numa CWL agora."""
    liga = requisitar_coc(f"/clans/{tag_para_url(tag_clan)}/currentwar/leaguegroup")
    if not liga or liga.get("state") in (None, "notInWar"):
        return None

    for rodada in liga.get("rounds", []):
        for war_tag in rodada.get("warTags", []):
            if not war_tag or war_tag == "#0":
                continue
            guerra = requisitar_coc(f"/clanwarleagues/wars/{tag_para_url(war_tag)}", timeout=20, tentativas=2)
            if not guerra:
                continue

            clan_info = guerra.get("clan", {})
            oponente_info = guerra.get("opponent", {})
            if clan_info.get("tag") == tag_clan:
                nosso, inimigo = clan_info, oponente_info
            elif oponente_info.get("tag") == tag_clan:
                nosso, inimigo = oponente_info, clan_info
            else:
                continue

            if guerra.get("state") in ("preparation", "inWar"):
                return {"guerra": guerra, "nosso": nosso, "inimigo": inimigo}
    return None


def relatorio_cwl_detalhado(tag_clan, apenas_pendentes=False, info=None, marcar_vinculados=True):
    """Monta a lista detalhada da rodada de CWL atual. Por padrão marca
    (@número) só de quem tem vínculo e ainda não atacou; com
    marcar_vinculados=False NENHUM membro é marcado, todos
    aparecem só com o nick. Retorna (texto, jids_mencionados) ou (None, [])."""
    if info is None:
        info = obter_guerra_cwl_atual(tag_clan)
    if not info or info["guerra"].get("state") != "inWar":
        return None, []

    guerra = info["guerra"]
    nosso = info["nosso"]
    inimigo = info["inimigo"]
    max_ataques = guerra.get("attacksPerMember", 1)

    membros = nosso.get("members", [])
    if apenas_pendentes:
        membros = [m for m in membros if len(m.get("attacks", []) or []) < max_ataques]
        if not membros:
            return None, []

    tags_pendentes = [m.get("tag") for m in membros if len(m.get("attacks", []) or []) < max_ataques]
    jids_por_tag = {}
    if marcar_vinculados:
        registros = carregar_registros()
        for m_tag in tags_pendentes:
            reg = registros.get(m_tag)
            if reg and reg.get("whatsapp_jid"):
                jids_por_tag[m_tag] = reg["whatsapp_jid"]

    if marcar_vinculados:
        linhas = [
            formatar_status_ataques_com_mencao(
                m.get("name"), m.get("attacks", []) or [], max_ataques, jids_por_tag.get(m.get("tag"))
            )
            for m in membros
        ]
    else:
        linhas = [
            formatar_status_ataques(m.get("name"), m.get("attacks", []) or [], max_ataques)
            for m in membros
        ]

    titulo = "⏳ *FALTAM ATACAR — CWL*" if apenas_pendentes else "⚔️ *STATUS DA CWL* ⚔️"
    cabecalho = f"🏛️ Clã: *{_nome_oficial_clan(tag_clan)}*\n\n"
    if not apenas_pendentes:
        destruicao_nosso = nosso.get("destructionPercentage", 0)
        destruicao_inimigo = inimigo.get("destructionPercentage", 0)
        ataques_usados = sum(len(m.get("attacks", []) or []) for m in nosso.get("members", []))
        ataques_possiveis = len(nosso.get("members", [])) * max_ataques
        cabecalho += (
            f"🛡️ *{nosso.get('name', 'Nosso Clã')}*  ⭐ {nosso.get('stars', 0)} "
            f"| 💥 {destruicao_nosso:.1f}%\n"
            f"🏴‍☠️ *{inimigo.get('name', 'Inimigo')}*  ⭐ {inimigo.get('stars', 0)} "
            f"| 💥 {destruicao_inimigo:.1f}%\n"
            f"⚔️ Ataques usados: {ataques_usados}/{ataques_possiveis}\n\n"
        )
    texto = f"{titulo}\n\n{cabecalho}" + "\n".join(linhas)

    return texto, list(jids_por_tag.values())


_alertas_cwl = {}  # warTag -> {"inicio": bool, "fim": bool}


def loop_avisos_fixos_cwl(grupo, tag):
    """Avisos automáticos fixos: 1h antes de começar e 1h antes de acabar a
    rodada de CWL em andamento."""
    if not avisos_auto_ligados(grupo, tag):
        return
    info = obter_guerra_cwl_atual(tag)
    if not info:
        return

    guerra = info["guerra"]
    chave = f"{tag}:{guerra.get('startTime')}"
    estado_alertas = _alertas_cwl.setdefault(chave, {"inicio": False, "fim": False})

    estado = guerra.get("state")
    agora = datetime.utcnow()

    if estado == "preparation":
        start_str = guerra.get("startTime")
        if start_str and not estado_alertas["inicio"]:
            try:
                start_time = datetime.strptime(start_str.split(".")[0], "%Y%m%dT%H%M%S")
                falta = start_time - agora
                if timedelta(minutes=59) <= falta <= timedelta(minutes=61):
                    inimigo = info["inimigo"].get("name", "Desconhecido")
                    msg = f"⚠️ *ATENÇÃO CLÃ!* A rodada da CWL de *{_nome_oficial_clan(tag, grupo)}* contra *{inimigo}* vai começar em *1 hora*!"
                    enviar_whatsapp(msg, grupo)
                    estado_alertas["inicio"] = True
            except Exception:
                pass

    elif estado == "inWar":
        end_str = guerra.get("endTime")
        if end_str and not estado_alertas["fim"]:
            try:
                end_time = datetime.strptime(end_str.split(".")[0], "%Y%m%dT%H%M%S")
                falta = end_time - agora
                if timedelta(minutes=59) <= falta <= timedelta(minutes=61):
                    msg = f"🚨 *ATENÇÃO CLÃ!* A rodada da CWL de *{_nome_oficial_clan(tag, grupo)}* está acabando! Falta apenas *1 hora*, corram para atacar!"
                    enviar_whatsapp(msg, grupo)
                    estado_alertas["fim"] = True
            except Exception:
                pass


def loop_aviso_periodico_cwl(grupo, tag):
    """Aviso de 4 em 4 horas durante a rodada de CWL em andamento.
    !avisosguerraon: lista detalhada de quem falta atacar (marcando vinculados).
    !avisosguerraoff: só a frase padrão, sem lista de nomes."""
    if not avisos_auto_ligados(grupo, tag):
        return
    if cwl_detalhado_ligado(grupo, tag):
        texto, jids = relatorio_cwl_detalhado(tag, apenas_pendentes=True)
        if texto:
            enviar_whatsapp(texto, grupo, mencionados=jids or None)
    else:
        info = obter_guerra_cwl_atual(tag)
        if info and info["guerra"].get("state") == "inWar":
            enviar_whatsapp(f"🏛️ Clã: *{_nome_oficial_clan(tag, grupo)}*\n\nNão se esqueçam de atacar na guerra!", grupo)


# ==========================================
# 3C. BÔNUS DA LIGA (relatório automático ao fim da CWL)
# ==========================================
def obter_participantes_bonus_liga(tag_clan):
    """Percorre todas as rodadas da CWL atual e retorna quem já atacou pelo
    menos uma vez (= recebe o bônus da liga). Retorna None se o clã não
    estiver numa CWL."""
    liga = requisitar_coc(f"/clans/{tag_para_url(tag_clan)}/currentwar/leaguegroup")
    if not liga or not liga.get("rounds"):
        return None

    temporada = liga.get("season")
    nome_cla = None
    participantes = {}
    todas_finalizadas = True

    for rodada in liga.get("rounds", []):
        for war_tag in rodada.get("warTags", []):
            if not war_tag or war_tag == "#0":
                continue
            guerra = requisitar_coc(f"/clanwarleagues/wars/{tag_para_url(war_tag)}", timeout=20, tentativas=2)
            if not guerra:
                todas_finalizadas = False
                continue
            if guerra.get("state") != "warEnded":
                todas_finalizadas = False

            clan_info = guerra.get("clan", {})
            oponente_info = guerra.get("opponent", {})
            if clan_info.get("tag") == tag_clan:
                nosso = clan_info
            elif oponente_info.get("tag") == tag_clan:
                nosso = oponente_info
            else:
                continue

            nome_cla = nosso.get("name", nome_cla)
            for m in nosso.get("members", []):
                p = participantes.setdefault(m.get("tag"), {"nome": m.get("name"), "atacou": False})
                if len(m.get("attacks", []) or []) > 0:
                    p["atacou"] = True

    return {
        "temporada": temporada,
        "nome_cla": nome_cla or tag_clan,
        "participantes": participantes,
        "todas_finalizadas": todas_finalizadas
    }


def loop_bonus_liga(grupo, tag):
    """Uma vez por dia, verifica se a CWL terminou e, se sim, manda a lista
    de quem recebeu o bônus da liga (só nick, sem marcação)."""
    if not avisos_auto_ligados(grupo, tag):
        return
    agora = agora_brasilia()
    if not (agora.hour == 13 and agora.minute == 0):
        return

    resultado = obter_participantes_bonus_liga(tag)
    if not resultado or not resultado["todas_finalizadas"] or not resultado["temporada"]:
        return

    status_geral = carregar_cwl_status()
    status = status_geral.setdefault(tag, {})
    if status.get("bonus_enviado") == resultado["temporada"]:
        return

    recebedores = [p["nome"] for p in resultado["participantes"].values() if p["atacou"]]
    if recebedores:
        texto = f"🎖️ *BÔNUS DA LIGA — {resultado['nome_cla']}*\n\n" + "\n".join(f"• {n}" for n in recebedores)
        enviar_whatsapp(texto, grupo)

    status["bonus_enviado"] = resultado["temporada"]
    status_geral[tag] = status
    salvar_cwl_status(status_geral)


# ==========================================
# ESTATÍSTICAS MENSAIS DE ESTRELAS (POR CLÃ)
# ==========================================
def is_clan_war_entry(war):
    if war.get("isFriendlyWar") is True:
        return False
    if str(war.get("warType", "")).lower() == "friendly":
        return False
    if str(war.get("type", "")).lower() == "friendly":
        return False
    if war.get("friendly") is True:
        return False
    return True


def parse_war_datetime(value):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1]
        if "." in value:
            value = value.split(".")[0]
        if "-" in value:
            return datetime.fromisoformat(value)
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    except Exception:
        return None


def atualizar_totais_estrelas(tag):
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/warlog")
    if not dados or "items" not in dados:
        return

    mes_atual = agora_brasilia().strftime("%Y-%m")
    estatisticas = carregar_estatisticas()
    clan_data = estatisticas.setdefault(tag, {})
    mes_data = clan_data.setdefault(mes_atual, {"players": {}, "war_tags": [], "report_sent": False})

    for war in dados["items"]:
        if not is_clan_war_entry(war):
            continue

        war_tag = war.get("warTag") or war.get("tag") or war.get("warId")
        if not war_tag or war_tag in mes_data["war_tags"]:
            continue

        data_guerra = parse_war_datetime(war.get("endTime") or war.get("startTime"))
        if not data_guerra or data_guerra.strftime("%Y-%m") != mes_atual:
            continue

        clan_info = war.get("clan", {})
        membros = clan_info.get("members", [])
        if not membros:
            continue

        for membro in membros:
            m_tag = membro.get("tag")
            if not m_tag:
                continue
            nome = membro.get("name", "Desconhecido")
            estrelas = membro.get("stars", 0) or 0

            player = mes_data["players"].setdefault(m_tag, {"name": nome, "stars": 0})
            player["name"] = nome
            player["stars"] += estrelas

        mes_data["war_tags"].append(war_tag)

    estatisticas[tag] = clan_data
    salvar_estatisticas(estatisticas)


def relatorio_estrelas_mensais(grupo, tag):
    if not avisos_auto_ligados(grupo, tag):
        return
    hoje = agora_brasilia().date()
    amanha = hoje + timedelta(days=1)
    if hoje.month == amanha.month:
        return

    mes_atual = hoje.strftime("%Y-%m")
    estatisticas = carregar_estatisticas()
    clan_data = estatisticas.get(tag, {})
    mes_data = clan_data.get(mes_atual)
    if not mes_data or mes_data.get("report_sent"):
        return

    players = mes_data.get("players", {})
    texto = (
        "🏆 *RELATÓRIO MENSAL DE ESTRELAS DE GUERRA* 🏆\n\n"
        f"🏛️ Clã: *{_nome_oficial_clan(tag, grupo)}*\n"
        f"📅 Mês: {hoje.strftime('%B %Y')}\n\n"
    )

    if not players:
        texto += "Nenhum registro de estrelas de guerra encontrado para este mês."
    else:
        ordenados = sorted(players.items(), key=lambda item: item[1].get("stars", 0), reverse=True)
        for _, info in ordenados:
            texto += f"{info.get('name', 'Desconhecido')} - {info.get('stars', 0)} ⭐\n"

    enviar_whatsapp(texto, grupo)
    mes_data["report_sent"] = True
    clan_data[mes_atual] = mes_data
    estatisticas[tag] = clan_data
    salvar_estatisticas(estatisticas)


# ==========================================
# 3D. DOAÇÕES
# ==========================================
# A API pública só expõe o contador ATUAL de doações do jogador, que reinicia
# periodicamente pelo próprio jogo (e também "some" se o jogador sai do clã).
# Por isso mantemos um banco interno: a cada rodada do loop, comparamos o
# valor bruto da API com o último valor visto e SOMAMOS a diferença num
# acumulado próprio por temporada — assim o total sobrevive a saídas/retornos
# do clã e a resets do próprio jogo.
def atualizar_doacoes(tag_clan):
    periodo_atual = identificador_temporada_atual()

    dados_clan_api = requisitar_coc(f"/clans/{tag_para_url(tag_clan)}")
    if not dados_clan_api:
        return

    doacoes_geral = carregar_doacoes()
    status = doacoes_geral.setdefault(tag_clan, {"temporada_atual": periodo_atual, "temporadas": {}})

    # Virada de temporada: a temporada atual vira "passada" e qualquer coisa
    # mais antiga que isso é descartada (só guardamos atual + passada).
    if status.get("temporada_atual") != periodo_atual:
        temporada_anterior_id = status.get("temporada_atual")
        temporadas_antigas = status.get("temporadas", {})
        nova_estrutura = {}
        if temporada_anterior_id and temporada_anterior_id in temporadas_antigas:
            nova_estrutura[temporada_anterior_id] = temporadas_antigas[temporada_anterior_id]
        status["temporadas"] = nova_estrutura
        status["temporada_atual"] = periodo_atual

    temporada_dados = status["temporadas"].setdefault(periodo_atual, {})

    for m in dados_clan_api.get("memberList", []):
        p_tag = m.get("tag")
        nome = m.get("name")
        doadas_raw = m.get("donations", 0) or 0
        recebidas_raw = m.get("donationsReceived", 0) or 0

        player = temporada_dados.setdefault(p_tag, {
            "nome": nome,
            "doadas": 0,
            "recebidas": 0,
            "ultimo_doadas_raw": doadas_raw,
            "ultimo_recebidas_raw": recebidas_raw
        })
        player["nome"] = nome

        delta_doadas = doadas_raw - player.get("ultimo_doadas_raw", 0)
        delta_recebidas = recebidas_raw - player.get("ultimo_recebidas_raw", 0)

        # Se o valor bruto caiu, o contador do jogo reiniciou nesse meio
        # tempo (reset do jogo, ou saiu/voltou do clã) — nesse caso o valor
        # atual já É o quanto foi feito desde o reset, então soma ele
        # inteiro em vez de uma diferença negativa.
        player["doadas"] += delta_doadas if delta_doadas >= 0 else doadas_raw
        player["recebidas"] += delta_recebidas if delta_recebidas >= 0 else recebidas_raw

        player["ultimo_doadas_raw"] = doadas_raw
        player["ultimo_recebidas_raw"] = recebidas_raw

    doacoes_geral[tag_clan] = status
    salvar_doacoes(doacoes_geral)


def comando_doacoes(tag=None):
    erro = _exigir_tag_cla(tag, "!doacoes#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados_clan_api = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    if not dados_clan_api:
        return "⚠️ Erro ao buscar dados do clã."

    periodo_atual = identificador_temporada_atual()
    doacoes_geral = carregar_doacoes()
    temporada_dados = doacoes_geral.get(clan_tag, {}).get("temporadas", {}).get(periodo_atual, {})

    ranking = []
    for m in dados_clan_api.get("memberList", []):
        p_tag = m.get("tag")
        info = temporada_dados.get(p_tag)
        if info:
            ranking.append((info.get("nome", m.get("name")), info.get("doadas", 0), info.get("recebidas", 0)))
        else:
            # Ainda não há snapshot dele nesta temporada (bot começou a rastrear agora)
            ranking.append((m.get("name"), 0, 0))

    if not ranking:
        return "⚠️ Nenhum membro encontrado no clã."

    ranking.sort(key=lambda item: item[1], reverse=True)
    linhas = [
        f"{i}. {nome} — 🎁 {doadas} doadas | 📥 {recebidas} recebidas"
        for i, (nome, doadas, recebidas) in enumerate(ranking, start=1)
    ]
    return f"🎁 *RANKING DE DOAÇÕES — TEMPORADA ATUAL*\n\n🏛️ Clã: *{_nome_oficial_clan(clan_tag)}*\n\n" + "\n".join(linhas)


def comando_doacoes_temporada_passada(tag=None):
    erro = _exigir_tag_cla(tag, "!doacoestemporadapassada#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    doacoes_geral = carregar_doacoes()
    status = doacoes_geral.get(clan_tag, {})
    temporadas = status.get("temporadas", {})
    periodo_atual = status.get("temporada_atual")

    periodo_passado = next((p for p in temporadas.keys() if p != periodo_atual), None)
    if not periodo_passado:
        return "⚠️ Ainda não há dados de uma temporada passada registrados."

    registros = carregar_registros()
    linhas_dados = []
    for p_tag, info in temporadas[periodo_passado].items():
        reg = registros.get(p_tag)
        if not reg:
            continue  # a lista detalhada da temporada passada só mostra vinculados
        linhas_dados.append((info.get("nome"), info.get("doadas", 0), info.get("recebidas", 0)))

    if not linhas_dados:
        data_fmt = datetime.strptime(periodo_passado, "%Y-%m-%d").strftime("%d/%m/%Y")
        return f"⚠️ Nenhum membro vinculado tinha doações registradas na temporada encerrada em {data_fmt}."

    linhas_dados.sort(key=lambda item: item[1], reverse=True)
    linhas = [
        f"{nome} — 🎁 {doadas} doadas | 📥 {recebidas} recebidas"
        for nome, doadas, recebidas in linhas_dados
    ]
    data_fmt = datetime.strptime(periodo_passado, "%Y-%m-%d").strftime("%d/%m/%Y")
    return f"🎁 *DOAÇÕES — TEMPORADA ENCERRADA EM {data_fmt}*\n\n🏛️ Clã: *{_nome_oficial_clan(clan_tag)}*\n\n" + "\n".join(linhas)


# ==========================================
# 3E. RELATÓRIO DE TROFÉUS (!trofeus)
# ==========================================
def comando_trofeus(tag=None):
    erro = _exigir_tag_cla(tag, "!trofeus#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    if not dados:
        return "⚠️ Erro ao buscar dados do clã."

    membros = sorted(dados.get("memberList", []), key=lambda m: m.get("trophies", 0), reverse=True)
    linhas = [f"{i}. {m.get('name')} — 🏆 {m.get('trophies', 0)}" for i, m in enumerate(membros, start=1)]
    if not linhas:
        return "⚠️ Nenhum membro encontrado no clã."
    return f"🏆 *RELATÓRIO DE TROFÉUS*\n\n🏛️ Clã: *{_nome_oficial_clan(clan_tag)}*\n\n" + "\n".join(linhas)


def loop_relatorio_trofeus(grupo, tag):
    """Dispara automaticamente assim que a temporada vira de verdade (última
    segunda-feira do mês, meia-noite horário do Leste dos EUA) — não no
    último dia do mês do calendário."""
    if not avisos_auto_ligados(grupo, tag):
        return
    temporada_atual_id = identificador_temporada_atual()
    status_geral = carregar_trofeus_status()
    status = status_geral.setdefault(tag, {})

    if status.get("temporada_atual") is None:
        # primeira vez rodando: só registra a temporada em andamento, sem disparar
        status["temporada_atual"] = temporada_atual_id
        status_geral[tag] = status
        salvar_trofeus_status(status_geral)
        return

    if status["temporada_atual"] == temporada_atual_id:
        return  # ainda estamos na mesma temporada, nada a fazer

    # A temporada virou desde a última checagem: manda o relatório final
    texto = comando_trofeus(tag)
    enviar_whatsapp(texto, grupo)
    status["temporada_atual"] = temporada_atual_id
    status_geral[tag] = status
    salvar_trofeus_status(status_geral)


# ==========================================
# 4. SISTEMA DE REGISTRO (VÍNCULO WHATSAPP <-> JOGADOR) E WEBHOOK
# ==========================================
def _extrair_comando_e_tag(mensagem_texto, prefixo):
    """
    Exige ESPAÇO após o comando (ex: '!perfil #TAG'). Retorna a tag em
    maiúsculas com '#' na frente, ou None se não houver tag (ou se o
    argumento veio colado ao comando sem espaço).
    """
    resto = mensagem_texto[len(prefixo):]
    if not resto:
        return None
    if not resto[0].isspace():
        return None
    resto = resto.strip()
    if not resto:
        return None
    tag = resto.upper()
    if not tag.startswith("#"):
        tag = "#" + tag.lstrip("#")
    return tag


def _separar_tag_jogador_e_referencia(texto, prefixo, chat_jid):
    """Para o comando '!registrar', onde a tag do JOGADOR sempre vem primeiro
    e, em seguida (com espaço), o apelido (ou #tag) do clã — obrigatório:
    '!registrar #TAG apelido' ou '!registrar #TAG #TAGDOCLA'. Exige espaço
    após o comando E entre os argumentos (nada de tag colada).
    Retorna (tag_jogador_normalizada, ref_do_cla) — ref pode ser apelido
    (ex: 'meucla') ou tag de clã (ex: '#ABC123'), ou None se não informado."""
    resto = texto[len(prefixo):]
    if not resto:
        return None, None
    if not resto[0].isspace():
        return None, None
    resto = resto.strip().lstrip(":").strip()
    if not resto:
        return None, None

    if " " in resto:
        parte_tag, parte_ref = resto.split(None, 1)
        tag_jogador = normalizar_tag(parte_tag)
        ref_cla = parte_ref.strip() or None
        return tag_jogador, ref_cla

    return normalizar_tag(resto), None


def processar_comando_registro(mensagem_texto, remetente_jid, chat_jid=None, admin_ja_autorizado=False):
    destino_resposta = chat_jid or remetente_jid
    if not mensagem_texto:
        return None

    texto_limpo = mensagem_texto.strip()
    texto_lower = _sem_acentos(texto_limpo).lower()

    # --- comandos de gerenciamento de clãs do grupo (checar ANTES de
    # !registrar, pois "!vincularcla" começa com prefixo parecido) ---
    # Todos exclusivos de admin: dono do bot, admin real do WhatsApp no
    # grupo, ou o admin cadastrado/promovido automaticamente para o grupo.
    if texto_lower.startswith("!vincularcla"):
        if not admin_ja_autorizado and not usuario_pode_administrar_grupo(chat_jid, remetente_jid):
            return TEXTO_BLOQUEIO_ADMIN
        resto = texto_limpo[len("!vincularcla"):]
        if resto and not resto[0].isspace():
            return "⚠️ Use um espaço após o comando. Ex: `!vincularcla #TAGDOCLA <apelido>`"
        resto = resto.strip().lstrip(":").strip()
        return comando_vincularcla(resto, chat_jid)

    if texto_lower.startswith("!registrar"):
        try:
            # Formato obrigatório: a *tag do jogador* primeiro e, com espaço,
            # o apelido (ou #tag) do clã — ex: '!registrar #TAG apelido' ou
            # '!registrar #TAG #TAGDOCLA'.
            tag_player, ref_cla = _separar_tag_jogador_e_referencia(
                texto_limpo, texto_limpo[:len("!registrar")], chat_jid
            )

            if not tag_player:
                return "⚠️ Use o formato correto: `!registrar #TAGJOGADOR <apelido do clã>` (com espaço). Ex: `!registrar #ABC123 meucla`"
            if not ref_cla:
                return "⚠️ Informe também o *apelido do clã* (ou a #tag do clã) após a tag do jogador, com espaço. Ex: `!registrar #ABC123 meucla` ou `!registrar #ABC123 #XYZ987`"

            resultado, dados_player = validar_tag_no_onboarding(tag_player, "jogador")
            if resultado == "nao_existe":
                return "❌ Tag inválida ou jogador não encontrado no Clash of Clans."
            if resultado == "tipo_errado":
                nome_outro = dados_player.get("name") if dados_player else ""
                return f"⚠️ Essa tag é de um *clã*{f' ({nome_outro})' if nome_outro else ''}, não de um jogador. Envie a *tag do jogador* (ex: #ABC123)."
            if resultado == "erro_api" or not dados_player:
                return "⚠️ A API do Clash of Clans não respondeu agora. Tente novamente em instantes."

            # Define o clã do vínculo (obrigatório no formato atual):
            # 1) ref_cla #tag → valida na API e usa esse clã;
            # 2) ref_cla apelido → apelido cadastrado no grupo (exige grupo).
            tag_clan = None
            nome_clan = None
            if ref_cla.strip().startswith("#"):
                tag_ref = normalizar_tag(ref_cla)
                # Só aceita clãs JÁ vinculados ao grupo: a #tag (ou o apelido)
                # precisa ser de um dos clãs cadastrados ali (via !vincularcla).
                if chat_jid and str(chat_jid).endswith("@g.us"):
                    clas_grupo = obter_clas_do_grupo(chat_jid)
                    if tag_ref.upper() not in (clas_grupo or {}):
                        return (
                            "❌ O clã da #tag não está vinculado a este grupo. O registro "
                            "de jogadores só aceita clãs já cadastrados aqui — envie o "
                            "*apelido* do clã cadastrado no grupo (veja `!vinculados`)."
                        )
                    tag_ref = tag_ref.upper()
                resultado_ref, dados_ref = validar_tag_no_onboarding(tag_ref, "cla")
                if resultado_ref == "nao_existe":
                    return f"❌ Não encontrei o clã {tag_ref} no Clash of Clans."
                if resultado_ref == "tipo_errado":
                    return "⚠️ O que veio depois da *tag do jogador* também é um jogador, não um clã. Para escolher o clã, use o *apelido* cadastrado no grupo ou a #tag do clã (ex: `#ABC123`)."
                if resultado_ref == "erro_api":
                    return "⚠️ A API do Clash of Clans não respondeu agora. Tente novamente em instantes."
                tag_clan = dados_ref.get("tag") or tag_ref
                nome_clan = dados_ref.get("name")
            else:
                # Apelido de clã deste grupo
                if not chat_jid or not str(chat_jid).endswith("@g.us"):
                    return "⚠️ Para usar o *apelido* do clã, envie o comando dentro do grupo (ex: `!registrar #TAG apelido`)."
                clas_grupo = obter_clas_do_grupo(chat_jid)
                tag_resolvido = resolver_alias_para_tag(chat_jid, ref_cla)
                if not clas_grupo or tag_resolvido not in clas_grupo:
                    return f"❌ O apelido *{ref_cla}* não corresponde a nenhum clã vinculado a este grupo. Peça ao admin para ver os vínculos (`!vinculados`)."
                tag_clan = tag_resolvido
                nome_clan = clas_grupo[tag_resolvido].get("nome") or tag_resolvido

            registros = carregar_registros()

            # Regra de unicidade: uma tag de jogador só pode ser cadastrada
            # UMA vez — mesmo que seja o próprio número repetindo, informa que
            # já foi registrado em vez de sobrescrever o vínculo existente.
            if tag_player in registros:
                return f"⚠️ A tag *{tag_player}* já foi registrada. Cada tag só pode ser cadastrada uma vez."

            if tag_player not in registros and contar_tags_do_numero(registros, remetente_jid) >= MAX_TAGS_POR_NUMERO:
                return f"⚠️ Você já vinculou o máximo de {MAX_TAGS_POR_NUMERO} tags a este número de WhatsApp."

            nome_player = dados_player.get("name")

            registros[tag_player] = {
                "whatsapp_jid": remetente_jid,
                "nome_coc": nome_player,
                "tag_clan": tag_clan,
                "nome_clan": nome_clan
            }
            salvar_registros(registros)

            # Confirmação de acordo com o que foi escolhido.
            if tag_clan:
                texto_vinculo = f" no clã *{nome_clan}* ({tag_clan})"
            else:
                texto_vinculo = " (sem clã vinculado no momento)"
            mensagem_confirmacao = (
                f"✅ Registrado com sucesso! O jogador *{nome_player}* ({tag_player}) foi vinculado ao seu "
                f"WhatsApp{texto_vinculo}."
            )
            try:
                enviar_whatsapp(mensagem_confirmacao, destino_resposta)
            except Exception:
                pass
        except Exception:
            return "⚠️ Algo deu errado ao processar seu registro. Tente novamente em instantes."

        # Envia também um mini-perfil uma única vez após o registro
        try:
            nivel_vila = dados_player.get("townHallLevel", "N/A")
            trofeus = dados_player.get("trophies", "N/A")
            liga = liga_do_player(dados_player)
            # Vila do Construtor
            builder_hall = dados_player.get("builderHallLevel", "N/A")
            builder_league = traduzir_liga(
                dados_player.get("versusLeague", {}).get("name")
                or dados_player.get("builderBaseLeague", {}).get("name")
            )
            builder_trophies = dados_player.get("versusTrophies")
            if builder_trophies is None:
                builder_trophies = dados_player.get("builderBaseTrophies", "N/A")
            mensagem_perfil = (
                f"📌 *Perfil de {nome_player}*\n\n"
                f"🔖 Tag: {tag_player}\n"
                f"🏰 Clã atual: {nome_clan} ({tag_clan})\n"
                f"🏠 Nível da Vila: {nivel_vila}\n"
                f"🏆 Troféus: {trofeus}\n"
                f"🥇 Liga Ranqueada: {liga}\n"
                f"🏗️ Vila do Construtor: BH {builder_hall} - {builder_league}\n"
                f"🔹 Troféus (Vila do Construtor): {builder_trophies}"
            )
            enviar_whatsapp(mensagem_perfil, destino_resposta)
        except Exception:
            pass

        return None

    if texto_lower.startswith("!perfil"):
        # !perfilcla (card do clã) é tratado nos comandos gerais — passa
        # direto por aqui para não ser interpretado como !perfil.
        if texto_lower.startswith("!perfilcla"):
            return None
        resto_perfil = texto_limpo[len("!perfil"):]
        if resto_perfil and not resto_perfil[0].isspace():
            return "⚠️ Use um espaço após o comando: `!perfil #TAG`"

        registros = carregar_registros()
        tag_player = _extrair_comando_e_tag(texto_limpo, "!perfil")

        tags_do_numero = [
            t for t, info in registros.items()
            if info.get("whatsapp_jid") == remetente_jid
        ]

        if not tag_player and not tags_do_numero:
            return "⚠️ Nenhuma tag fornecida e nenhum registro encontrado para seu número. Use `!perfil #SUATAG` ou registre com `!registrar #TAGJOGADOR <apelido do clã>`."

        # Sem tag e com mais de uma conta vinculada: mostra o resumo do perfil.
        if not tag_player and len(tags_do_numero) > 1:
            return _montar_resumo_perfil(tags_do_numero, registros, remetente_jid)

        if not tag_player:
            tag_player = tags_do_numero[0]

        tag_encoded = tag_player.replace("#", "%23")
        dados_player = requisitar_coc(f"/players/{tag_encoded}")

        if not dados_player:
            return "❌ Tag inválida ou jogador não encontrado no Clash of Clans."

        return _montar_perfil_individual(dados_player, tag_player, registros.get(tag_player) or {})

    return None


def _montar_perfil_individual(dados_player, tag_player, info_registro):
    """Monta o perfil detalhado de um jogador (formato usado quando o !perfil
    é chamado com uma tag específica ou quando há apenas uma conta vinculada)."""
    nome_player = dados_player.get("name", "Desconhecido")
    nivel_vila = dados_player.get("townHallLevel", "N/A")
    estrelas_guerra = dados_player.get("warStars", "N/A")
    liga = liga_do_player(dados_player)
    trofeus = dados_player.get("trophies", "N/A")
    recorde_trofeus = dados_player.get("bestTrophies", "N/A")
    doadas = dados_player.get("donations", "N/A")
    recebidas = dados_player.get("donationsReceived", "N/A")
    nivel_jogador = dados_player.get("expLevel", "N/A")

    clan_atual = dados_player.get("clan") or {}
    if clan_atual.get("name"):
        clan_atual_txt = f"{clan_atual.get('name')} ({clan_atual.get('tag')})"
    else:
        clan_atual_txt = "Nenhum clã no momento"

    cargo = traduzir_cargo(dados_player.get("role"))

    return (
        f"👤 *Informações do Perfil:* {nome_player} ({tag_player})\n"
        f"🏰 Centro da Vila (Town Hall): {nivel_vila}\n"
        f"⭐ Estrelas de Guerra: {estrelas_guerra}\n"
        f"⚔️ Liga: {liga}\n"
        f"🏛️ Clã: {clan_atual_txt}\n"
        f"🎖️ Cargo: {cargo}\n"
        f"🏆 Troféus: {trofeus}\n"
        f"🏅 Recorde de Troféus: {recorde_trofeus}\n"
        f"🎯 Tropas Doadas: {doadas}\n"
        f"📥 Tropas Recebidas: {recebidas}\n"
        f"👥 Nível do Jogador: {nivel_jogador}\n"
        f"🗡️ Clã vinculado: {_txt_cla_vinculado(info_registro, clan_atual)}"
    )


def _txt_cla_vinculado(info_registro, clan_atual):
    """Retorna o texto indicando o clã vinculado no registro (o apelido ou a
    tag escolhidos no !registrar). Se o jogador marcou o clã atual, mostra só o
    atual; caso contrário tenta o nome/tag gravados."""
    nome_clan_reg = info_registro.get("nome_clan")
    tag_clan_reg = info_registro.get("tag_clan")
    apelido_clan_reg = info_registro.get("apelido_clan")
    if tag_clan_reg and clan_atual.get("tag") and tag_clan_reg == clan_atual.get("tag"):
        nome_exibido = nome_clan_reg or apelido_clan_reg or clan_atual.get("name")
        return f"{nome_exibido} ({tag_clan_reg})"
    if apelido_clan_reg:
        return f"{apelido_clan_reg} ({tag_clan_reg})" if tag_clan_reg else apelido_clan_reg
    if nome_clan_reg:
        return f"{nome_clan_reg} ({tag_clan_reg})" if tag_clan_reg else nome_clan_reg
    return "Nenhum"


def _montar_resumo_perfil(tags_do_numero, registros, remetente_jid):
    """Resumo exibido quando o !perfil é digitado sem tag e o número tem mais
    de uma conta vinculada: lista as contas e os clãs com nível e guerras."""
    numero_curto = remetente_jid.split("@")[0]

    linhas_contas = []
    clas_com_tags = {}
    for indice, tag in enumerate(tags_do_numero, start=1):
        tag_encoded = tag.replace("#", "%23")
        dados_player = requisitar_coc(f"/players/{tag_encoded}")
        if not dados_player:
            linhas_contas.append(f"{indice}. {tag}\n❌ Não foi possível buscar o jogador.")
            continue
        nome_player = dados_player.get("name", "Desconhecido")
        th = dados_player.get("townHallLevel", "?")
        trofeus = dados_player.get("trophies", "?")
        liga_player = liga_do_player(dados_player)
        linhas_contas.append(
            f"{indice}. {nome_player} ({tag})\nCV{th} | 🏆 {trofeus} | ⚔️ {liga_player}"
        )

        clan_jogador = dados_player.get("clan") or {}
        tag_clan = clan_jogador.get("tag")
        if tag_clan:
            clas_com_tags.setdefault(tag_clan, {"nome": clan_jogador.get("name"), "indices": []})
            clas_com_tags[tag_clan]["indices"].append(indice)

    texto_clas = _montar_lista_clas_resumo(clas_com_tags)

    return (
        "👤 *RESUMO DO PERFIL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📱 Usuário: {numero_curto}\n"
        "🖇️ Contas vinculadas\n"
        + "\n".join(linhas_contas)
        + "\n────────────────────\n"
        "🏰 Clãs:\n"
        + texto_clas
    )


def _montar_lista_clas_resumo(clas_com_tags):
    """Busca o nível e o número de guerras ganhas de cada clã na API e monta
    a listagem '🏰 Clãs:' do resumo do perfil."""
    if not clas_com_tags:
        return "Nenhum clã vinculado."

    linhas = []
    for posicao, (tag_clan, info) in enumerate(clas_com_tags.items(), start=1):
        tag_encoded = tag_clan.replace("#", "%23")
        dados_clan = requisitar_coc(f"/clans/{tag_encoded}")
        if dados_clan:
            nome_clan = dados_clan.get("name") or info.get("nome") or tag_clan
            nivel = dados_clan.get("clanLevel", "?")
            guerras = dados_clan.get("warWins", "?")
            linhas.append(
                f"{posicao}. 🏰 {nome_clan} ({tag_clan})\n"
                f"🎖️ Nível {nivel} | ⭐ {guerras} guerras ganhas"
            )
        else:
            linhas.append(f"{posicao}. 🏰 {info.get('nome') or tag_clan} ({tag_clan})")

    return "\n".join(linhas)


app = Flask(__name__)


# ==========================================
# COMANDOS GERAIS DO CLÃ (!cla, !membros, !guerra, !capital, etc.)
# ==========================================
def _extrair_tag_apos_prefixo(texto, prefixo):
    resto = texto[len(prefixo):]
    if not resto:
        return None
    if not resto[0].isspace():
        return None
    resto = resto.strip().lstrip(":").strip()
    if not resto:
        return None
    tag = resto.upper()
    if not tag.startswith("#"):
        tag = "#" + tag.lstrip("#")
    return tag


def _exigir_tag_cla(tag, exemplo_comando):
    """Retorna uma mensagem de erro padrão se o apelido/tag do clã não foi
    informado, ou None se estiver tudo certo para prosseguir. A mensagem é
    montada a partir do exemplo passado (ex: '!doacoes#TAGDOCLA') e aplicada
    a TODOS os comandos que exigem clã — sempre exigindo ESPAÇO após o
    comando e indicando que aceita o apelido cadastrado OU a #tag."""
    if not tag:
        base = (exemplo_comando or "").split("#")[0].rstrip()
        return (
            f"⚠️ Informe o apelido do clã ou a tag, com um espaço após o comando. "
            f"Ex: `{base} <apelido>` ou `{base} #TAGDOCLA`"
        )
    return None


def resolver_alias_para_tag(chat_jid, valor):
    """Converte um apelido de clã (curto, cadastrado via !vincularcla) na
    tag longa correspondente daquele grupo. Se o valor não bater com nenhum
    apelido, devolve o próprio valor (que é tratado como tag normal)."""
    if not valor or not chat_jid:
        return valor
    clas = obter_clas_do_grupo(chat_jid)
    alvo = valor.upper().lstrip("#")
    for tag, info in clas.items():
        apelido = (info.get("apelido") or "").strip().upper()
        if apelido and apelido == alvo:
            return tag
    return valor


def _extrair_tag_resolvido(texto, prefixo, chat_jid):
    """Extrai a tag após o prefixo do comando e resolve apelidos de clã
    configurados no grupo antes de devolver."""
    tag = _extrair_tag_apos_prefixo(texto, prefixo)
    if not tag:
        return None
    return resolver_alias_para_tag(chat_jid, tag)


def _exigir_cla_do_grupo(chat_jid, tag):
    """Retorna uma mensagem de erro se a tag não for um dos clãs cadastrados
    no grupo informado (comandos !cla/!membros/!cvs só
    mostram informações dos clãs monitorados naquele grupo). Retorna None se
    o clã for válido."""
    if tag is None or not chat_jid or not str(chat_jid).endswith("@g.us"):
        return None
    clas = obter_clas_do_grupo(chat_jid)
    if tag in clas:
        return None
    return f"⚠️ O clã *{tag}* não está registrado neste grupo. Peça ao administrador para cadastrá-lo."


_cache_nome_oficial_clan = {}


def _nome_oficial_clan(tag, chat_jid=None):
    """Nome OFICIAL do clã (o nome de verdade dentro do jogo, vindo da API),
    usado em relatórios e avisos automáticos — em vez do apelido cadastrado
    ou da tag. A busca é cacheada por 30 minutos; se a API falhar, cai de
    volta para o nome salvo em grupos_clas.json e, por último, para a tag."""
    agora = time.time()
    cache = _cache_nome_oficial_clan.get(tag)
    if cache and agora - cache[0] < 1800:
        return cache[1]

    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}")
    if dados and dados.get("name"):
        _cache_nome_oficial_clan[tag] = (agora, dados["name"])
        return dados["name"]

    nome_salvo = None
    if chat_jid:
        clas = obter_clas_do_grupo(chat_jid)
        info = clas.get(tag)
        if info and info.get("nome"):
            nome_salvo = info["nome"]
    if not nome_salvo:
        nome_salvo = tag
    _cache_nome_oficial_clan[tag] = (agora, nome_salvo)
    return nome_salvo


def comando_cla(tag=None):
    erro = _exigir_tag_cla(tag, "!cla#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    if not dados:
        return "⚠️ Erro ao buscar dados do clã."

    vitorias = dados.get("warWins")
    derrotas = dados.get("warLosses")
    empates = dados.get("warTies")
    sequencia = dados.get("warWinStreak")

    if vitorias is None:
        guerra_txt = "Registro de guerras indisponível (clã com histórico privado)."
    elif derrotas is None or empates is None:
        guerra_txt = f"Vitórias: {vitorias} (derrotas/empates indisponíveis - histórico privado)"
    else:
        total = vitorias + derrotas + empates
        guerra_txt = f"Vitórias: {vitorias} | Derrotas: {derrotas} | Empates: {empates} | Total de guerras: {total}"
    if sequencia is not None:
        guerra_txt += f"\n🔥 Vitórias seguidas (atual): {sequencia}"

    liga_api = (dados.get("warLeague") or {}).get("name")
    liga_cwl = liga_cwl_curta(liga_api)
    liga_cwl_txt = f"🏆 Liga da CWL: {liga_cwl}\n" if liga_api else ""

    texto = (
        f"🏰 *{dados.get('name')}*\n"
        f"#Tag: {clan_tag}\n"
        f"🔗 Link: {gerar_link_clan(clan_tag)}\n"
        f"Nível: {dados.get('clanLevel')}\n"
        f"Troféus: {dados.get('clanPoints')}\n"
        f"{liga_cwl_txt}"
        f"Membros: {dados.get('members')}/50\n\n"
        f"⚔️ *Guerras*\n{guerra_txt}"
    )
    descricao = dados.get("description")
    if descricao:
        texto += f"\n\n📝 {descricao}"
    return texto


def comando_membros(tag=None):
    erro = _exigir_tag_cla(tag, "!membros#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    if not dados:
        return "⚠️ Erro ao buscar membros do clã."
    membros = dados.get("memberList", [])
    linhas = [
        f"{i}. {m.get('name')} [{_abreviar_cargo(m.get('role'))}]"
        for i, m in enumerate(membros, start=1)
    ]
    return "👥 *MEMBROS DO CLÃ*\n\n" + "\n".join(linhas)


def comando_cvs(tag=None):
    erro = _exigir_tag_cla(tag, "!cvs#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    if not dados:
        return "⚠️ Erro ao buscar composição do clã."
    membros = dados.get("memberList", [])
    contagem = {}
    for m in membros:
        th = m.get("townHallLevel", "?")
        contagem[th] = contagem.get(th, 0) + 1

    def chave_ordenacao(item):
        th = item[0]
        return th if isinstance(th, int) else -1

    linhas = [f"TH{th}: {qtd}" for th, qtd in sorted(contagem.items(), key=chave_ordenacao, reverse=True)]
    return "🏘️ *TOTAL DE CV (COMPOSIÇÃO)*\n\n" + "\n".join(linhas)


def comando_clan_externo(tag):
    if not tag:
        return "⚠️ Use o formato correto: `!clans #TAG`"
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}")
    if not dados:
        return f"⚠️ Clã {tag} não encontrado."
    return comando_cla(tag)


def _carregar_fonte_perfil(tamanho, negrito=False):
    """Carrega uma fonte TTF para o card do clã. Procura fontes comuns do
    Windows e do Linux (Docker); se nada existir, usa a fonte embutida do
    Pillow (Aileron), que aceita tamanho."""
    from PIL import ImageFont
    candidatos = []
    if os.name == "nt":
        candidatos = [
            (r"C:\Windows\Fonts\arialbd.ttf" if negrito else r"C:\Windows\Fonts\arial.ttf"),
            (r"C:\Windows\Fonts\segoeuib.ttf" if negrito else r"C:\Windows\Fonts\segoeui.ttf"),
            (r"C:\Windows\Fonts\verdana.ttf"),
        ]
    candidatos += [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrito else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if negrito else "/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    ]
    for caminho in candidatos:
        if caminho and os.path.exists(caminho):
            try:
                return ImageFont.truetype(caminho, tamanho)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:
        return ImageFont.load_default()


def _baixar_brasao(url_brasao):
    """Baixa o brasão do clã (badgeUrls da API) e devolve como Image RGBA,
    ou None se a imagem não puder ser baixada."""
    from PIL import Image
    from io import BytesIO
    try:
        resposta = requests.get(url_brasao, timeout=10)
        if resposta.ok:
            return Image.open(BytesIO(resposta.content)).convert("RGBA")
    except Exception as e:
        print(f"Erro ao baixar brasão do clã: {e}")
    return None


def _quebrar_linhas(draw, texto, fonte, largura_max):
    """Quebra um texto em linhas que cabem em largura_max pixels."""
    linhas = []
    for paragrafo in (texto or "").split("\n"):
        atual = ""
        for palavra in (paragrafo or "").split(" "):
            teste = (atual + " " + palavra).strip()
            if draw.textlength(teste, font=fonte) <= largura_max:
                atual = teste
            else:
                if atual:
                    linhas.append(atual)
                atual = palavra
        linhas.append(atual)
    return linhas


def _gerar_imagem_perfil_cla(dados):
    """Desenha o 'card' do clã (estilo card web) com Pillow: brasão, nome,
    tag, nível, informações gerais, etiquetas e descrição. Devolve
    o caminho do PNG gerado ou None em caso de falha."""
    from PIL import Image, ImageDraw

    nome = dados.get("name") or "CLÃ"
    tag = dados.get("tag") or ""
    nivel = dados.get("clanLevel") or 0
    descricao = dados.get("description") or ""
    local = (dados.get("location") or {}).get("name") or "—"
    liga = liga_cwl_curta((dados.get("warLeague") or {}).get("name"))
    pontos = dados.get("clanPoints") or 0
    vitorias = dados.get("clanWins") or 0
    empates = dados.get("clanTies") or 0
    derrotas = dados.get("clanLosses") or 0
    sequencia = dados.get("warWinStreak") or 0
    total_membros = dados.get("members") or len(dados.get("memberList") or [])
    etiquetas = [rotulo.get("name") for rotulo in (dados.get("labels") or []) if rotulo.get("name")]

    largura = 1080
    margem = 44
    x_texto = margem + 24

    COR_FUNDO = (20, 24, 39)
    COR_CARTAO = (33, 40, 62)
    COR_DOURADO = (226, 185, 59)
    COR_TEXTO = (242, 245, 252)
    COR_CINZA = (158, 168, 190)
    COR_LINHA = (52, 60, 88)

    fonte_nome = _carregar_fonte_perfil(46, negrito=True)
    fonte_tag = _carregar_fonte_perfil(26)
    fonte_nivel = _carregar_fonte_perfil(24, negrito=True)
    fonte_secao = _carregar_fonte_perfil(28, negrito=True)
    fonte_label = _carregar_fonte_perfil(26)
    fonte_valor = _carregar_fonte_perfil(26, negrito=True)
    fonte_chip = _carregar_fonte_perfil(22)
    fonte_desc = _carregar_fonte_perfil(24)

    # Canvas alto o suficiente; no final recorta na altura real usada.
    imagem = Image.new("RGB", (largura, 2500), COR_FUNDO)
    draw = ImageDraw.Draw(imagem)
    y = 40

    # ---- Brasão (ou placeholder com a inicial do nome) ----
    url_brasao = (
        (dados.get("badgeUrls") or {}).get("large")
        or (dados.get("badgeUrls") or {}).get("medium")
        or (dados.get("badgeUrls") or {}).get("small")
    )
    brasao = _baixar_brasao(url_brasao) if url_brasao else None
    lado_brasao = 118
    x_brasao = margem
    if brasao:
        try:
            brasao = brasao.resize((lado_brasao, lado_brasao), Image.LANCZOS)
            mascara = Image.new("L", (lado_brasao, lado_brasao), 0)
            desenho_mascara = ImageDraw.Draw(mascara)
            desenho_mascara.ellipse((0, 0, lado_brasao, lado_brasao), fill=255)
            imagem.paste(brasao, (x_brasao, y), mascara)
        except Exception as e:
            print(f"Erro ao montar brasão: {e}")
            brasao = None
    if not brasao:
        draw.rounded_rectangle(
            (x_brasao, y, x_brasao + lado_brasao, y + lado_brasao),
            radius=26, fill=COR_DOURADO
        )
        inicial = (nome[:1] or "?").upper()
        draw.text((x_brasao + lado_brasao // 2, y + lado_brasao // 2), inicial,
                  font=_carregar_fonte_perfil(52, negrito=True),
                  fill=(20, 24, 39), anchor="mm")

    # ---- Nome, tag e nível ----
    x_nome = x_brasao + lado_brasao + 28
    draw.text((x_nome, y + 4), nome, font=fonte_nome, fill=COR_TEXTO)
    draw.text((x_nome, y + 62), tag, font=fonte_tag, fill=COR_CINZA)

    texto_nivel = f"NÍVEL {nivel}" if nivel else "NÍVEL ?"
    larg_nivel = draw.textlength(texto_nivel, font=fonte_nivel)
    altura_nivel = 40
    chip_x1 = x_nome
    chip_y1 = y + 96
    draw.rounded_rectangle(
        (chip_x1, chip_y1, chip_x1 + larg_nivel + 36, chip_y1 + altura_nivel),
        radius=altura_nivel // 2, fill=COR_DOURADO
    )
    draw.text((chip_x1 + 18, chip_y1 + altura_nivel // 2), texto_nivel,
              font=fonte_nivel, fill=(20, 24, 39), anchor="lm")

    y = y + lado_brasao + 44

    # ---- Separador ----
    draw.line((margem, y, largura - margem, y), fill=COR_LINHA, width=2)
    y += 36

    # ---- Destaque: vitórias seguidas (streak) ----
    if sequencia:
        texto_streak = f"🔥 {sequencia} vitória{'s' if sequencia != 1 else ''} seguidas"
        larg_streak = draw.textlength(texto_streak, font=fonte_secao) + 40
        altura_streak = 56
        draw.rounded_rectangle(
            (x_texto, y, x_texto + larg_streak, y + altura_streak),
            radius=altura_streak // 2, fill=COR_DOURADO
        )
        draw.text((x_texto + 20, y + altura_streak // 2), texto_streak,
                  font=fonte_secao, fill=(20, 24, 39), anchor="lm")
        y += altura_streak + 28

    # ---- INFORMAÇÕES ----
    def desenhar_info(rotulo, valor):
        nonlocal y
        draw.text((x_texto, y), rotulo, font=fonte_label, fill=COR_CINZA)
        draw.text((largura - margem, y), str(valor), font=fonte_valor,
                  fill=COR_TEXTO, anchor="ra")
        y += 54

    draw.text((x_texto, y), "INFORMAÇÕES", font=fonte_secao, fill=COR_DOURADO)
    y += 44
    desenhar_info("Liga da CWL", liga)
    desenhar_info("Total de troféus", f"{pontos:,}".replace(",", "."))
    desenhar_info("Local", local)
    desenhar_info("Vitórias / Empates / Derrotas", f"{vitorias} / {empates} / {derrotas}")
    desenhar_info("Sequência de vitórias", sequencia)
    desenhar_info("Membros", f"{total_membros} / 50")

    # ---- ETIQUETAS ----
    if etiquetas:
        y += 8
        draw.text((x_texto, y), "ETIQUETAS", font=fonte_secao, fill=COR_DOURADO)
        y += 42
        altura_chip = 44
        gap = 14
        x_chip = x_texto
        for nome_etiqueta in etiquetas:
            larg_chip = draw.textlength(nome_etiqueta, font=fonte_chip) + 40
            if x_chip + larg_chip > largura - margem:
                x_chip = x_texto
                y += altura_chip + gap
            draw.rounded_rectangle(
                (x_chip, y, x_chip + larg_chip, y + altura_chip),
                radius=altura_chip // 2, fill=COR_CARTAO, outline=COR_DOURADO, width=2
            )
            draw.text((x_chip + 20, y + altura_chip // 2), nome_etiqueta,
                      font=fonte_chip, fill=COR_TEXTO, anchor="lm")
            x_chip += larg_chip + gap
        y += altura_chip + 30

    # ---- DESCRIÇÃO ----
    if descricao:
        draw.line((margem, y, largura - margem, y), fill=COR_LINHA, width=2)
        y += 36
        draw.text((x_texto, y), "DESCRIÇÃO", font=fonte_secao, fill=COR_DOURADO)
        y += 44
        for linha_desc in _quebrar_linhas(draw, descricao, fonte_desc, largura - 2 * x_texto):
            draw.text((x_texto, y), linha_desc, font=fonte_desc, fill=COR_TEXTO)
            y += 38
        y += 12

    # ---- Rodapé ----
    y += 16
    draw.line((margem, y, largura - margem, y), fill=COR_LINHA, width=2)
    draw.text((largura // 2, y + 28), f"{nome} • {tag} — card do clã",
              font=_carregar_fonte_perfil(20), fill=COR_CINZA, anchor="ma")

    imagem = imagem.crop((0, 0, largura, y + 72))
    try:
        caminho = os.path.join(tempfile.gettempdir(), f"perfil_cla_{tag.replace('#', '')}.png")
        imagem.save(caminho)
        return caminho
    except Exception as e:
        print(f"Erro ao salvar imagem do clã: {e}")
        return None


def _texto_perfil_cla(dados):
    """Versão em texto do perfil do clã (fallback quando a imagem não puder
    ser gerada/enviada)."""
    nome = dados.get("name") or "?"
    tag = dados.get("tag") or ""
    liga = liga_cwl_curta((dados.get("warLeague") or {}).get("name"))
    etiquetas = [r.get("name") for r in (dados.get("labels") or []) if r.get("name")]
    partes = [
        f"🛡️ *{nome}* ({tag})",
        f"Nível: {dados.get('clanLevel') or '?'}",
        f"Local: {(dados.get('location') or {}).get('name') or '—'}",
        f"Liga da CWL: {liga}",
        f"Total de troféus: {dados.get('clanPoints') or 0}",
        f"Guerras: {dados.get('clanWins') or 0}V / {dados.get('clanTies') or 0}E / {dados.get('clanLosses') or 0}D | Sequência: {dados.get('warWinStreak') or 0}",
        f"Membros: {dados.get('members') or 0}/50",
    ]
    if etiquetas:
        partes.append("Etiquetas: " + ", ".join(etiquetas[:6]))
    if dados.get("description"):
        partes.append(f"\n{dados['description']}")
    return "🛡️ *PERFIL DO CLÃ* 🛡️\n\n" + "\n".join(partes)


def comando_perfil_cla(tag=None, chat_jid=None):
    """!perfilcla: busca o perfil completo do clã na API (brasão, etiquetas,
    liga, capital etc.) e envia um card em imagem direto no grupo. Se a
    imagem não puder ser gerada/enviada, cai para o texto do perfil."""
    erro = _exigir_tag_cla(tag, "!perfilcla#TAGDOCLA")
    if erro:
        return erro
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}")
    if not dados:
        return f"⚠️ Clã {tag} não encontrado."

    try:
        caminho = _gerar_imagem_perfil_cla(dados)
    except Exception as e:
        import traceback
        print(f"Erro ao gerar imagem do clã {tag}: {e}")
        traceback.print_exc()
        caminho = None
    if caminho:
        legenda = f"{dados.get('name')} ({dados.get('tag')}) — Nível {dados.get('clanLevel') or '?'}"
        if chat_jid and enviar_imagem_whatsapp(caminho, chat_jid, legenda=legenda):
            try:
                os.remove(caminho)
            except Exception:
                pass
            return None
    return _texto_perfil_cla(dados)


def _relatorio_guerra_random(dados):
    """Monta o relatório da GUERRA RANDOM (guerra normal, fora da CWL) a
    partir dos dados do endpoint /currentwar. Retorna o texto identificando
    claramente que se trata de uma 'GUERRA RANDOM', ou None se não houver
    guerra normal em preparação/andamento."""
    estado = dados.get("state")
    if estado not in ("preparation", "inWar"):
        return None

    nosso = dados.get("clan", {})
    inimigo = dados.get("opponent", {})
    nome_nosso = nosso.get("name", "Nosso Clã")
    nome_inimigo = inimigo.get("name", "Inimigo")

    if estado == "preparation":
        return (
            "🛡️ *GUERRA RANDOM — DIA DE PREPARAÇÃO* 🛡️\n\n"
            f"🛡️ *{nome_nosso}*\n"
            f"🏴‍☠️ Oponente: *{nome_inimigo}*\n\n"
            "A guerra ainda não começou — preparem as armas! ⚔️"
        )

    max_ataques = dados.get("attacksPerMember", 2)
    linhas = [
        formatar_status_ataques(m.get("name"), m.get("attacks", []) or [], max_ataques)
        for m in nosso.get("members", [])
    ]
    return (
        "⚔️ *GUERRA RANDOM EM ANDAMENTO* ⚔️\n\n"
        f"🛡️ *{nome_nosso}*  ⭐ {nosso.get('stars', 0)}\n"
        f"🏴‍☠️ *{nome_inimigo}*  ⭐ {inimigo.get('stars', 0)}\n\n"
        "📋 *Membros:*\n" + "\n".join(linhas)
    )


def _relatorio_cwl_preparacao(info):
    """Texto da rodada de CWL em fase de preparação (quando o /currentwar
    ainda não considera a rodada)."""
    nosso = info["nosso"]
    inimigo = info["inimigo"]
    return (
        "🛡️ *CWL — DIA DE PREPARAÇÃO* 🛡️\n\n"
        f"🛡️ *{nosso.get('name', 'Nosso Clã')}*\n"
        f"🏴‍☠️ Oponente: *{inimigo.get('name', 'Inimigo')}*\n\n"
        "A rodada da CWL ainda não começou — preparem as armas! ⚔️"
    )


def _guerra_random_e_a_mesma_cwl(dados_currentwar, info_cwl):
    """True quando o /currentwar devolve exatamente a rodada de CWL que já
    foi encontrada (mesmo início e oponentes) — evita reportar a MESMA
    guerra duas vezes (uma como CWL e outra como GUERRA RANDOM)."""
    if not dados_currentwar or not info_cwl:
        return False
    guerra_cwl = info_cwl.get("guerra", {})
    mesmo_inicio = bool(
        dados_currentwar.get("startTime")
        and dados_currentwar.get("startTime") == guerra_cwl.get("startTime")
    )
    tags_cwl = {
        info_cwl.get("nosso", {}).get("tag"),
        info_cwl.get("inimigo", {}).get("tag"),
    }
    oponente_random = dados_currentwar.get("opponent", {}).get("tag")
    if oponente_random in tags_cwl:
        return True
    return bool(mesmo_inicio)


def comando_guerra(tag=None):
    erro = _exigir_tag_cla(tag, "!guerra#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag

    info = obter_guerra_cwl_atual(clan_tag)
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}/currentwar")
    if not dados:
        return "⚠️ Erro ao buscar dados da guerra."

    texto_cwl = None
    if info:
        estado_cwl = info["guerra"].get("state")
        if estado_cwl == "preparation":
            texto_cwl = _relatorio_cwl_preparacao(info)
        elif estado_cwl == "inWar":
            texto_cwl, _ = relatorio_cwl_detalhado(clan_tag, apenas_pendentes=False, info=info)

    if info and _guerra_random_e_a_mesma_cwl(dados, info):
        return texto_cwl if texto_cwl else "⚔️ O clã está em CWL no momento."

    texto_random = _relatorio_guerra_random(dados)
    partes = []
    if texto_cwl:
        partes.append(texto_cwl)
    if texto_random:
        partes.append(texto_random)
    if partes:
        return "\n\n".join(partes)
    if info:
        return "⚔️ O clã está em CWL no momento."
    return "⚔️ O clã não está em guerra no momento."


def comando_historico(tag=None):
    erro = _exigir_tag_cla(tag, "!historico#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}/warlog")
    if not dados or "items" not in dados:
        return "⚠️ Não foi possível buscar o histórico (pode estar privado nas configurações do clã)."
    linhas = []
    for item in dados["items"][:10]:
        emoji = {"win": "🟢", "lose": "🔴", "tie": "🟡"}.get(item.get("result"), "⚪")
        nosso_estrelas = item.get("clan", {}).get("stars", 0)
        inimigo_estrelas = item.get("opponent", {}).get("stars", 0)
        inimigo = item.get("opponent", {}).get("name", "?")
        linhas.append(
            f"{emoji} *{_nome_oficial_clan(clan_tag)}* {nosso_estrelas}⭐ x "
            f"{inimigo_estrelas}⭐ *{inimigo}*"
        )
    return (
        f"📜 *HISTÓRICO DE GUERRAS (últimas 10)*\n\n"
        f"🏛️ Clã: *{_nome_oficial_clan(clan_tag)}*\n\n"
        + "\n".join(linhas)
        + "\n\n🟢 vitória • 🟡 empate • 🔴 derrota"
    )


def comando_capital(tag=None):
    erro = _exigir_tag_cla(tag, "!capital#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag
    dados_clan = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}")
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}/capitalraidseasons")
    if not dados_clan:
        return "⚠️ Erro ao buscar dados do clã."
    if not dados or "items" not in dados or not dados["items"]:
        return "⚠️ Erro ao buscar dados da Capital."

    temporada = dados["items"][0]
    estado_pt = {"ongoing": "Em andamento", "ended": "Encerrada"}.get(temporada.get("state"), temporada.get("state"))

    capital = dados_clan.get("clanCapital") or {}
    capital_nivel = capital.get("capitalHallLevel") or "?"
    distritos = capital.get("districts") or []

    total_construcoes = len(distritos)
    concluidas = sum(1 for d in distritos if d.get("districtHallLevel") or 0 >= 5)
    faltando = total_construcoes - concluidas

    membros_raide = temporada.get("members") or []
    textos_ouro = []
    for m in membros_raide[:8]:
        nome = m.get("name", "?")
        ataques = m.get("attacks", 0)
        ouro = m.get("capitalResourcesLooted", 0)
        textos_ouro.append(f"{nome}: {ataques} ataques | 🪙 {ouro or 0}")
    if len(membros_raide) > 8:
        textos_ouro.append(f"... +{len(membros_raide) - 8} membros")

    linhas = [
        "🏛️ *CAPITAL DO CLÃ*",
        f"🏛️ Clã: *{_nome_oficial_clan(clan_tag)}* ({clan_tag})",
        f"🏗️ Capital: *nível {capital_nivel}* — construções: ✅ {concluidas} concluídas | ⏳ {faltando} faltando | 🧱 {total_construcoes} total",
        f"Status da raide: {estado_pt}",
        f"🪙 Ouro total saqueado: {temporada.get('capitalTotalLoot', 0)}",
        f"⚔️ Ataques realizados: {temporada.get('totalAttacks', 0)}",
        f"💥 Distritos inimigos destruídos: {temporada.get('enemyDistrictsDestroyed', 0)}",
        f"🏆 Recompensa ofensiva: {temporada.get('offensiveReward', 0)} | Defensiva: {temporada.get('defensiveReward', 0)}",
    ]
    if textos_ouro:
        linhas.append("\n💰 *Ouro por atacante:*\n" + "\n".join(textos_ouro))
    return "\n".join(linhas)


def comando_vinculados(chat_jid):
    """!vinculados (admin/dono): lista os clãs vinculados ao grupo E os
    membros com WhatsApp vinculado."""
    partes = []

    grupos = carregar_grupos_clas()
    clas_grupo = grupos.get(chat_jid, {})
    if clas_grupo:
        linhas_clas = []
        for tag, info in clas_grupo.items():
            apelido = info.get("apelido")
            nome = info.get("nome") or tag
            linhas_clas.append(
                f"• *{nome}* ({tag})" + (f" — apelido *{apelido}*" if apelido else "")
            )
        partes.append(
            "🏘️ *CLÃS VINCULADOS A ESTE GRUPO*\n\n"
            + "\n".join(linhas_clas)
            + f"\n\n🔢 Total: {len(clas_grupo)} de {MAX_CLAS_POR_GRUPO} clãs"
        )
    else:
        partes.append("🏘️ Nenhum clã vinculado a este grupo ainda. Use `!vincularcla #TAGDOCLA <apelido>` para cadastrar.")

    registros = carregar_registros()
    if registros:
        linhas_membros = []
        for tag, info in registros.items():
            jid = info.get("whatsapp_jid", "")
            nick = info.get("nome_coc", "Desconhecido")
            nome_contato = buscar_nome_contato(jid)
            identificacao = nome_contato or (jid.split("@")[0] if jid else "?")
            nome_clan = info.get("nome_clan")
            clan_txt = f" — {nome_clan}" if nome_clan else ""
            linhas_membros.append(f"{nick} — {tag}{clan_txt} — {identificacao}")
        partes.append("🔗 *MEMBROS VINCULADOS*\n\n" + "\n".join(linhas_membros))
    else:
        partes.append("🔗 Nenhum membro vinculou o WhatsApp ainda.")

    return "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n\n".join(partes)


def registrar_cla_no_grupo(tag, chat_jid, apelido=None):
    """Registra o clã no grupo (grupos_clas.json) e promove automaticamente
    o admin que assinou o serviço no privado para esta tag, se houver.
    Retorna a mensagem de resultado."""
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}")
    if not dados:
        return f"⚠️ Clã {tag} não encontrado."

    grupos = carregar_grupos_clas()
    clas_grupo = grupos.setdefault(chat_jid, {})
    primeiro_cla_do_grupo = not clas_grupo

    if tag in clas_grupo:
        return f"⚠️ O clã *{dados.get('name')}* ({tag}) já está registrado neste grupo."
    if len(clas_grupo) >= MAX_CLAS_POR_GRUPO:
        return f"⚠️ Este grupo já atingiu o limite de {MAX_CLAS_POR_GRUPO} clãs registrados."

    novo = {"guerra_on": False, "aviso_cwl_detalhado": False, "avisos_auto": False, "nome": dados.get("name")}
    if apelido:
        novo["apelido"] = apelido
    clas_grupo[tag] = novo
    grupos[chat_jid] = clas_grupo
    salvar_grupos_clas(grupos)

    msg = f"✅ Clã *{dados.get('name')}* ({tag}) registrado neste grupo."
    if apelido:
        msg += f"\n📛 Apelido *{apelido}* também poderá ser usado no lugar da tag."

    # Se alguém assinou o serviço no chat privado com esta tag de clã, esse
    # número é reconhecido automaticamente como o admin cadastrado deste
    # grupo agora que sabemos o chat_jid real — sem precisar de comando manual.
    admin_promovido = promover_admin_pendente_se_houver(tag, chat_jid)
    if admin_promovido:
        msg += (
            f"\n👤 Número {admin_promovido.split('@')[0]} reconhecido automaticamente "
            f"como administrador cadastrado deste grupo (painel no privado liberado)."
        )

    if primeiro_cla_do_grupo:
        msg += (
            "\n\n🎉 *Primeiro clã vinculado! Os comandos do grupo foram liberados.*\n\n"
            "🛡️ *Administradores:* digite `!comandosadm` para gerenciar o bot "
            "e ativar os *avisos automáticos de guerra/CWL*, entre outros.\n\n"
            "👥 *Membros:* digite `!comandos` para vincular a #tag ao seu número, "
            "ver detalhes das guerras/CWL, troféus e doações sem precisar entrar no jogo.\n\n"
            "⚠️ Avisos de *Guerra/CWL* só começam depois que o administrador ativar "
            "(`!avisosguerraon`). Lembretes de *Raide da Capital* e "
            "*Jogos do Clã* já são automáticos."
        )

    return msg


def _parse_tag_e_apelido(resto):
    """Interpreta o argumento de !vincularcla em (tag, apelido), EXIGINDO
    espaço entre eles: '!vincularcla #TAG apelido'. Tag primeiro, apelido
    depois. Apelido pode ser None se não vier separado por espaço."""
    if not resto or not resto.strip():
        return None, None
    partes = resto.split(None, 1)
    tag = "#" + partes[0].strip().lstrip("#").upper()
    apelido = partes[1].strip() if len(partes) > 1 else None
    return tag, apelido


def comando_vincularcla(argumento, chat_jid):
    """!vincularcla: vincula um clã ao grupo com espaço obrigatório entre
    tag e apelido — ex: '!vincularcla #TAGDOCLA meucla'."""
    tag, apelido = _parse_tag_e_apelido(argumento)
    if not tag:
        return "⚠️ Use o formato correto: `!vincularcla #TAGDOCLA <apelido>` (com espaço entre a tag e o apelido)"
    if not apelido:
        return "⚠️ Use o formato correto: `!vincularcla #TAGDOCLA <apelido>` (o apelido é obrigatório, com espaço após a tag)"
    if not chat_jid or not str(chat_jid).endswith("@g.us"):
        return "⚠️ Esse comando só pode ser usado dentro de um grupo."
    return registrar_cla_no_grupo(tag, chat_jid, apelido)


def comando_avisosguerraoff(tag, chat_jid):
    if not tag:
        return "⚠️ Use o formato correto: `!avisosguerraoff <apelido>`"
    if not chat_jid:
        return "⚠️ Esse comando só pode ser usado dentro de um grupo."

    grupos = carregar_grupos_clas()
    clas_grupo = grupos.get(chat_jid, {})
    if tag not in clas_grupo:
        return f"❌ Clã {tag} não está registrado neste grupo. Use `!vincularcla` primeiro."

    clas_grupo[tag]["guerra_on"] = False
    clas_grupo[tag]["avisos_auto"] = False
    clas_grupo[tag]["aviso_cwl_detalhado"] = False
    grupos[chat_jid] = clas_grupo
    salvar_grupos_clas(grupos)
    return f"🔕 Avisos automáticos desativados para o clã {tag} neste grupo (guerra/CWL de 4/4h, raide e jogos)."


def comando_avisosguerraon(tag, chat_jid):
    if not tag:
        return "⚠️ Use o formato correto: `!avisosguerraon <apelido>`"
    if not chat_jid:
        return "⚠️ Esse comando só pode ser usado dentro de um grupo."

    grupos = carregar_grupos_clas()
    clas_grupo = grupos.get(chat_jid, {})
    if tag not in clas_grupo:
        return f"❌ Clã {tag} não está registrado neste grupo. Use `!vincularcla` primeiro."

    clas_grupo[tag]["guerra_on"] = True
    clas_grupo[tag]["avisos_auto"] = True
    clas_grupo[tag]["aviso_cwl_detalhado"] = True
    grupos[chat_jid] = clas_grupo
    salvar_grupos_clas(grupos)
    return f"🔔 Avisos automáticos ativados para o clã {tag} neste grupo (guerra/CWL de 4/4h, raide e jogos)."


def _relatorio_faltantes_guerra_random_com_mencao(dados):
    """Relatório de quem AINDA NÃO ATACOU na GUERRA RANDOM, marcando
    @número apenas para quem tem o WhatsApp vinculado E ainda não completou
    os ataques — quem já atacou (mesmo com vínculo) e quem não tem vínculo
    aparecem só com o nick do jogo. Retorna (texto, jids_mencionados) ou
    (None, []) se não houver guerra normal em andamento."""
    estado = dados.get("state")
    if estado != "inWar":
        return None, []
    nosso = dados.get("clan", {})
    inimigo = dados.get("opponent", {})
    max_ataques = dados.get("attacksPerMember", 2)

    membros = nosso.get("members", [])
    faltantes = [m for m in membros if len(m.get("attacks", []) or []) < max_ataques]
    if not faltantes:
        return None, []

    jids_por_tag = {}
    registros = carregar_registros()
    for m in faltantes:
        reg = registros.get(m.get("tag"))
        if reg and reg.get("whatsapp_jid"):
            jids_por_tag[m.get("tag")] = reg["whatsapp_jid"]

    linhas = [
        formatar_status_ataques_com_mencao(
            m.get("name"), m.get("attacks", []) or [], max_ataques, jids_por_tag.get(m.get("tag"))
        )
        for m in faltantes
    ]
    texto = (
        "⏳ *AVISO — FALTAM ATACAR NA GUERRA RANDOM* ⏳\n\n"
        f"🛡️ *{nosso.get('name', 'Nosso Clã')}*  ⭐ {nosso.get('stars', 0)}\n"
        f"🏴‍☠️ *{inimigo.get('name', 'Inimigo')}*  ⭐ {inimigo.get('stars', 0)}\n\n"
        + "\n".join(linhas)
    )
    return texto, list(jids_por_tag.values())


def comando_atacar(tag=None, chat_jid=None):
    """!atacar: marca (@número) apenas quem tem o WhatsApp vinculado E ainda
    não completou os ataques — quem já atacou (mesmo com vínculo) e quem não
    tem vínculo aparecem só com o nick. Cobre GUERRA RANDOM e CWL."""
    erro = _exigir_tag_cla(tag, "!atacar#TAGDOCLA")
    if erro:
        return erro
    clan_tag = tag

    info = obter_guerra_cwl_atual(clan_tag)
    dados = requisitar_coc(f"/clans/{tag_para_url(clan_tag)}/currentwar")
    if not dados:
        return "⚠️ Erro ao buscar dados da guerra."

    partes = []
    jids_mencionados = []
    if info and info["guerra"].get("state") == "inWar":
        texto_cwl, jids_cwl = relatorio_cwl_detalhado(clan_tag, apenas_pendentes=True, info=info)
        if texto_cwl:
            partes.append(texto_cwl)
            jids_mencionados.extend(jids_cwl)
    if not (info and _guerra_random_e_a_mesma_cwl(dados, info)):
        texto_random, jids_random = _relatorio_faltantes_guerra_random_com_mencao(dados)
        if texto_random:
            partes.append(texto_random)
            jids_mencionados.extend(jids_random)

    if not partes:
        return "✅ Todo mundo já usou todos os ataques (GUERRA RANDOM e CWL)!"

    texto = "\n\n".join(partes)
    if jids_mencionados and chat_jid:
        enviar_whatsapp(texto, chat_jid, mencionados=jids_mencionados)
        return None
    return texto


def _fmt_decimal(valor, casas=2):
    """Formata número com vírgula decimal (padrão brasileiro): 39.4 -> '39,40'."""
    return f"{float(valor or 0):.{casas}f}".replace(".", ",")


def _fmt_duracao(segundos):
    """Segundos (duração de ataque da API) no formato '2 min 13 s'."""
    segundos = int(round(segundos or 0))
    minutos, seg = divmod(segundos, 60)
    if minutos:
        return f"{minutos} min {seg} s"
    return f"{seg} s"


def _melhor_defesa_do_lado(lado, inimigo):
    """A melhor defesa deste lado: entre os ataques que o INIMIGO deu contra
    membros deste lado, o que fez MENOS estrelas/destruição (o defensor que
    melhor segurou). Retorna texto formatado ou 'Nenhum'."""
    candidatos = []
    for defensor in (lado.get("members", []) or []):
        tag_defensor = defensor.get("tag")
        for atacante in (inimigo.get("members", []) or []):
            for atk in (atacante.get("attacks", []) or []):
                if atk.get("defenderTag") and atk.get("defenderTag") == tag_defensor:
                    candidatos.append((defensor.get("name"), atk))
    if not candidatos:
        return "Nenhum"
    nome, atk = min(
        candidatos,
        key=lambda c: ((c[1].get("stars") or 0), (c[1].get("destructionPercentage") or 0)),
    )
    return f"🛡️ {nome} — {atk.get('stars') or 0}⭐ {_fmt_decimal(atk.get('destructionPercentage') or 0, 1)}%"


def _calcular_estatisticas_ataques(lado, ataques_por_membro=2):
    """Calcula as estatísticas de ataques de UM lado da guerra a partir do
    objeto 'clan'/'opponent' do /currentwar (ou da rodada de CWL). Retorna um
    dict com contagens, distribuição de estrelas, médias e o melhor ataque."""
    membros = lado.get("members", []) or []
    ataques = []
    for m in membros:
        for atk in (m.get("attacks", []) or []):
            ataques.append({"membro": m.get("name"), "atk": atk})

    usados = len(ataques)
    total_possivel = ataques_por_membro * len(membros)

    soma_estrelas = sum((a["atk"].get("stars") or 0) for a in ataques)
    soma_destruicao = sum((a["atk"].get("destructionPercentage") or 0) for a in ataques)
    duracoes = [a["atk"].get("duration") for a in ataques if a["atk"].get("duration")]

    dist = {3: 0, 2: 0, 1: 0, 0: 0}
    for a in ataques:
        estrelas = int(a["atk"].get("stars") or 0)
        dist[estrelas] = dist.get(estrelas, 0) + 1

    melhor_ataque = None
    for a in ataques:
        chave = ((a["atk"].get("stars") or 0), (a["atk"].get("destructionPercentage") or 0))
        if melhor_ataque is None or chave > melhor_ataque[0]:
            melhor_ataque = (chave, a["membro"])
    if melhor_ataque:
        (estrelas, destruicao), nome = melhor_ataque
        texto_melhor_ataque = f"👤 {nome} — {estrelas}⭐ {_fmt_decimal(destruicao, 1)}%"
    else:
        texto_melhor_ataque = "Nenhum"

    return {
        "membros": len(membros),
        "usados": usados,
        "restantes": max(0, total_possivel - usados),
        "vencidos": sum(1 for a in ataques if (a["atk"].get("stars") or 0) >= 1),
        "perdidos": dist.get(0, 0),
        "dist": dist,
        "estrelas": lado.get("stars", 0),
        "destruicao_total": lado.get("destructionPercentage", 0) or 0,
        "media_estrelas": (soma_estrelas / usados) if usados else 0,
        "media_destruicao": (soma_destruicao / usados) if usados else 0,
        "media_duracao": _fmt_duracao(sum(duracoes) / len(duracoes)) if duracoes else "—",
        "melhor_ataque": texto_melhor_ataque,
    }


def comando_status(tag=None):
    """!status: estatísticas detalhadas da guerra/CWL em andamento — serve
    tanto para a GUERRA normal quanto para a CWL. Mostra os DOIS lados lado a
    lado — placar, destruição total, total de ataques (usados/vencidos/perdidos/
    restantes), distribuição de estrelas, médias (estrelas, destruição,
    duração) e destaques (melhor ataque/defesa)."""
    erro = _exigir_tag_cla(tag, "!status#TAGDOCLA")
    if erro:
        return erro

    info = obter_guerra_cwl_atual(tag)
    dados = requisitar_coc(f"/clans/{tag_para_url(tag)}/currentwar")

    guerra = None
    if info and info["guerra"].get("state") == "inWar":
        guerra = info["guerra"]
    elif dados and dados.get("state") == "inWar" and not _guerra_random_e_a_mesma_cwl(dados, info):
        guerra = dados
    if not guerra:
        return "⚔️ Não há guerra (random ou CWL) em andamento neste momento. As estatísticas só aparecem com a guerra rolando."

    nosso = guerra.get("clan", {})
    inimigo = guerra.get("opponent", {})
    ataques_por_membro = guerra.get("attacksPerMember", 2)

    est_n = _calcular_estatisticas_ataques(nosso, ataques_por_membro)
    est_e = _calcular_estatisticas_ataques(inimigo, ataques_por_membro)
    est_n["melhor_defesa"] = _melhor_defesa_do_lado(nosso, inimigo)
    est_e["melhor_defesa"] = _melhor_defesa_do_lado(inimigo, nosso)

    nome_nosso = nosso.get("name", "Nosso Clã")
    nome_inimigo = inimigo.get("name", "Inimigo")

    return (
        "⚔️ *ESTATÍSTICAS DE GUERRA / CWL* ⚔️\n\n"
        f"🛡️ Confronto: *{nome_nosso}* ({est_n['membros']}) vs *{nome_inimigo}* ({est_e['membros']})\n"
        f"🏆 Placar: {est_n['estrelas']} nos - {est_e['estrelas']} eles\n"
        f"💥 Destruição Total: {_fmt_decimal(est_n['destruicao_total'], 2)}% — {_fmt_decimal(est_e['destruicao_total'], 2)}%\n\n"
        "📊 *Total de Ataques*\n"
        f"Ataques usados: {est_n['usados']} | {est_e['usados']}\n"
        f"Atk vencidos: {est_n['vencidos']} | {est_e['vencidos']}\n"
        f"Atk perdidos: {est_n['perdidos']} | {est_e['perdidos']}\n"
        f"Atk restantes: {est_n['restantes']} | {est_e['restantes']}\n\n"
        "🌟 *Melhores Ataques*\n"
        f"3 estrelas: {est_n['dist'][3]} | {est_e['dist'][3]}\n"
        f"2 estrelas: {est_n['dist'][2]} | {est_e['dist'][2]}\n"
        f"1 estrela: {est_n['dist'][1]} | {est_e['dist'][1]}\n"
        f"0 estrela: {est_n['dist'][0]} | {est_e['dist'][0]}\n\n"
        "📈 *Estatísticas de Ataques*\n"
        f"Média de ⭐ por ataque: {_fmt_decimal(est_n['media_estrelas'], 2)} | {_fmt_decimal(est_e['media_estrelas'], 2)}\n"
        f"Média de destruição: {_fmt_decimal(est_n['media_destruicao'], 1)}% | {_fmt_decimal(est_e['media_destruicao'], 1)}%\n"
        f"Duração média do atk: {est_n['media_duracao']} | {est_e['media_duracao']}\n\n"
        "🏅 *Batalhas Apresentadas*\n"
        f"Ataque mais heróico: {est_n['melhor_ataque']} | {est_e['melhor_ataque']}\n"
        f"Defesa mais heróica: {est_n['melhor_defesa']} | {est_e['melhor_defesa']}"
    )


TEXTO_AJUDA_COMANDOS = (
    "⚔️ *COMANDOS DO CLASH OF CLANS* ⚔️\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 *Jogador (até 5 tags por número)*\n"
    "• !registrar\n"
    "• !perfil\n\n"
    "🏰 *Clã do grupo*\n"
    "• !perfilcla\n"
    "• !cla\n"
    "• !membros\n"
    "• !cvs\n"
    "• !clans (buscar outro clã)\n\n"
    "⚔️ *Guerra*\n"
    "• !guerra\n"
    "• !atacar\n"
    "• !historico\n"
    "• !status\n\n"
    "🏛️ *Capital*\n"
    "• !capital\n\n"
    "🎁 *Doações*\n"
    "• !doacoes\n"
    "• !doacoestemporadapassada\n\n"
    "🏆 *Troféus*\n"
    "• !trofeus\n\n"
    "💡 No lugar do apelido também pode ser usada a tag completa (ex: `!perfilcla meucla` ou `!membros #TAGDOCLA`).\n"
    "⚠️ Para todos os comandos: *obrigatório o espaço* (ex: `!guerra <apelido>`).\n"
    "📋 Para saber a função de cada comando digite `!detalhes`."
)

TEXTO_DETALHES_COMANDOS = (
    "📚 *DETALHES DOS COMANDOS* 📚\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "📋 *Jogador (até 5 tags por número)*\n"
    "• !registrar #TAGJOGADOR <apelido do clã> — vincula seu WhatsApp à tag do jogador no clã indicado. Ex: `!registrar #ABC123 meucla` (o apelido ou a #tag do clã é obrigatório)\n"
    "• !perfil — perfil completo (TH, liga ranqueada exata, troféus, doações, clã vinculado); se tiver mais de uma conta vinculada mostra o resumo de todas\n\n"
    "🏰 *Clã do grupo*\n"
    "• !perfilcla <apelido> — card do clã (brasão, etiquetas, liga, capital)\n"
    "• !cla <apelido> — resumo do clã (nível, troféus, liga da CWL, membros, vitórias/empates/derrotas, sequência de vitórias e descrição)\n"
    "• !membros <apelido> — lista de membros com cargos abreviados\n"
    "• !cvs <apelido> — composição de CVs (quantidade por TH)\n"
    "• !clans #TAGDOCLA — busca outro clã pela tag\n\n"
    "⚔️ *Guerra*\n"
    "• !guerra <apelido> — relatório da GUERRA RANDOM e da CWL\n"
    "• !atacar <apelido> — avisa quem ainda não atacou (marca @ de quem tem vínculo; quem não tem aparece só com o nick)\n"
    "• !historico <apelido> — histórico das últimas 10 guerras\n"
    "• !status <apelido> — estatísticas detalhadas da guerra/CWL em andamento (placar, destruição, ataques usados/vencidos/perdidos/restantes, distribuição de estrelas e médias) dos dois lados — serve tanto para a guerra normal quanto para a CWL\n\n"
    "🏛️ *Capital*\n"
    "• !capital <apelido> — capital do clã (nível, construções concluídas/faltando/total, status e resultados da raide)\n\n"
    "🎁 *Doações*\n"
    "• !doacoes <apelido> — doações da temporada atual\n"
    "• !doacoestemporadapassada <apelido> — doações da temporada passada\n\n"
    "🏆 *Troféus*\n"
    "• !trofeus <apelido> — troféus atuais dos membros\n\n"
    "💡 No lugar do apelido também pode ser usada a tag completa (ex: `!perfilcla meucla` ou `!membros #TAGDOCLA`).\n"
    "🛡️ Comandos de administração do grupo (cadastrar clã, ligar/desligar avisos) são exclusivos do *administrador* — digite `!comandosadm`."
)

TEXTO_AJUDA_COMANDOS_ADMIN = (
    "🛡️ *COMANDOS DO ADMINISTRADOR* 🛡️\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🏘️ *Clãs do grupo (limite de 5 por grupo):*\n"
    "• !vincularcla #TAGDOCLA <apelido>\n"
    "• !vinculados — Lista todos os clãs e membros vinculados ao grupo.\n"
    "• !desvincularcla\n\n"
    "⚔️ *Guerra / CWL / Liga:*\n"
    "• !avisosguerraon <apelido ou tag>\n"
    "• !avisosguerraoff <apelido ou tag do clan>\n\n"
    "💡 Dica: Todos os comandos aceitam o apelido cadastrado no lugar da tag longa."
)


def processar_comandos_gerais(mensagem_texto, chat_jid, remetente_jid=None, admin_ja_autorizado=False):
    texto = mensagem_texto.strip()
    texto_lower = _sem_acentos(texto).lower()

    def _bloqueado_para_admin():
        return not admin_ja_autorizado and not usuario_pode_administrar_grupo(chat_jid, remetente_jid)

    # !comandosadm precisa ser checado antes de !comandos (mesmo prefixo).
    # Exclusivo do admin: outros membros recebem um aviso de acesso restrito.
    if texto_lower.startswith("!comandosadm"):
        if _bloqueado_para_admin():
            return (
                "🔒 *Acesso restrito!*\n\n"
                "O comando `!comandosadm` só pode ser usado pelos "
                "*administradores do grupo*. Os membros podem ver a lista "
                "de comandos disponíveis com `!comandos`."
            )
        return TEXTO_AJUDA_COMANDOS_ADMIN
    if texto_lower.startswith("!detalhes"):
        return TEXTO_DETALHES_COMANDOS
    if texto_lower.startswith("!comandos") or texto_lower.startswith("!ajuda"):
        return TEXTO_AJUDA_COMANDOS

    # !vincularcla: vincula um clã ao grupo, com espaço obrigatório entre a
    # tag e o apelido (exclusivo do admin e só no grupo)
    if texto_lower.startswith("!vincularcla"):
        if not chat_jid or not str(chat_jid).endswith("@g.us"):
            return "⚠️ O comando `!vincularcla` só pode ser usado dentro de um grupo."
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        resto = texto[len("!vincularcla"):]
        if resto and not resto[0].isspace():
            return "⚠️ Use um espaço após o comando. Ex: `!vincularcla #TAGDOCLA <apelido>`"
        resto = resto.strip().lstrip(":").strip()
        return comando_vincularcla(resto, chat_jid)

    # !desvincularplay (jogador) precisa ser checado antes de !desvincularcla
    if texto_lower.startswith("!desvincularplay"):
        if not chat_jid or not str(chat_jid).endswith("@g.us"):
            return "⚠️ O comando `!desvincularplay` só pode ser usado dentro de um grupo."
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        return iniciar_fluxo_desvincularplay(chat_jid, remetente_jid)

    # !desvincularcla: lista os clãs do grupo numerados para escolher qual excluir
    if texto_lower.startswith("!desvincularcla"):
        if not chat_jid or not str(chat_jid).endswith("@g.us"):
            return "⚠️ O comando `!desvincularcla` só pode ser usado dentro de um grupo."
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        return iniciar_fluxo_desvincularcla(chat_jid, remetente_jid)

    # !vinculados lista clãs + membros (admin/dono), e precisa ser checado
    # antes de !clans/!cla por ordem de bloco (sem conflito direto de prefixo)
    if texto_lower.startswith("!vinculados"):
        if not chat_jid or not str(chat_jid).endswith("@g.us"):
            return "⚠️ O comando `!vinculados` só pode ser usado dentro de um grupo."
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        return comando_vinculados(chat_jid)

    # !clans precisa ser checado antes de !cla (mesmo prefixo)
    if texto_lower.startswith("!clans"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!clans")], chat_jid)
        return comando_clan_externo(tag)
    if texto_lower.startswith("!cla"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!cla")], chat_jid)
        erro = _exigir_cla_do_grupo(chat_jid, tag)
        if erro:
            return erro
        return comando_cla(tag)

    # !perfilcla: card do clã (brasão, etiquetas, liga, capital...) em imagem
    if texto_lower.startswith("!perfilcla"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!perfilcla")], chat_jid)
        erro = _exigir_cla_do_grupo(chat_jid, tag)
        if erro:
            return erro
        return comando_perfil_cla(tag, chat_jid)

    if texto_lower.startswith("!membros"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!membros")], chat_jid)
        erro = _exigir_cla_do_grupo(chat_jid, tag)
        if erro:
            return erro
        return comando_membros(tag)
    if texto_lower.startswith("!cvs"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!cvs")], chat_jid)
        erro = _exigir_cla_do_grupo(chat_jid, tag)
        if erro:
            return erro
        return comando_cvs(tag)

    # !avisosguerraoff / !avisosguerraon precisam ser checados antes de !guerra
    if texto_lower.startswith("!avisosguerraoff"):
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        tag = _extrair_tag_resolvido(texto, texto[:len("!avisosguerraoff")], chat_jid)
        return comando_avisosguerraoff(tag, chat_jid)
    if texto_lower.startswith("!avisosguerraon"):
        if _bloqueado_para_admin():
            return TEXTO_BLOQUEIO_ADMIN
        tag = _extrair_tag_resolvido(texto, texto[:len("!avisosguerraon")], chat_jid)
        return comando_avisosguerraon(tag, chat_jid)
    if texto_lower.startswith("!historico") and not texto_lower.startswith("!historicoguerra"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!historico")], chat_jid)
        return comando_historico(tag)
    if texto_lower.startswith("!guerra"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!guerra")], chat_jid)
        return comando_guerra(tag)

    # Comandos de CWL: !avisoscwlon/!avisoscwloff foram removidos — a CWL
    # segue o mesmo liga/desliga do !avisosguerraon/!avisosguerraoff.
    if texto_lower.startswith("!atacar"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!atacar")], chat_jid)
        return comando_atacar(tag, chat_jid=chat_jid)

    if texto_lower.startswith("!capital"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!capital")], chat_jid)
        return comando_capital(tag)
    if texto_lower.startswith("!doacoestemporadapassada"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!doacoestemporadapassada")], chat_jid)
        return comando_doacoes_temporada_passada(tag)
    if texto_lower.startswith("!doacoes"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!doacoes")], chat_jid)
        return comando_doacoes(tag)
    if texto_lower.startswith("!trofeus"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!trofeus")], chat_jid)
        return comando_trofeus(tag)

    # !status: estatísticas detalhadas da guerra/CWL em andamento (serve
    # tanto para a guerra normal quanto para a CWL)
    if texto_lower.startswith("!status"):
        tag = _extrair_tag_resolvido(texto, texto[:len("!status")], chat_jid)
        return comando_status(tag)

    return None


# ==========================================
# 4.5 FLUXOS DE DESVÍNCULO (!desvincularcla e !desvincularplay, NO GRUPO)
# ==========================================
# Exclusivos de administradores: listam os itens numerados e aguardam o
# admin responder com o número do item que deseja desvincular. O estado fica
# salvo em ARQUIVO_FLUXO_VINCULAR, chaveado pelo chat_jid.

def iniciar_fluxo_desvincularcla(chat_jid, remetente_jid):
    """!desvincularcla (só admin): lista os clãs vinculados, numerando-os, e
    pede para o admin selecionar pelo número o clã que deseja excluir."""
    grupos = carregar_grupos_clas()
    clas_grupo = grupos.get(chat_jid, {})
    if not clas_grupo:
        return "🏘️ Nenhum clã vinculado a este grupo ainda. Use `!vincularcla #TAGDOCLA <apelido>` para cadastrar."

    fluxos = carregar_fluxo_vincular()
    fluxos[chat_jid] = {
        "tipo": "desvincularcla",
        "admin": remetente_jid,
        "clas": list(clas_grupo.keys()),
    }
    salvar_fluxo_vincular(fluxos)

    linhas = []
    for indice, tag in enumerate(clas_grupo, start=1):
        info = clas_grupo[tag]
        nome = info.get("nome") or tag
        linhas.append(f"{indice}. *{nome}* ({tag})")
    texto_lista = "\n".join(linhas)

    return (
        "🗑️ *DESVINCULAR CLÃ*\n\n"
        f"{texto_lista}\n\n"
        "Selecione o Clã que deseja excluir (envie o *número* correspondente).\n\n"
        "💡 Digite `!vinculados` para ver esta lista sem precisar excluir."
    )


def iniciar_fluxo_desvincularplay(chat_jid, remetente_jid):
    """!desvincularplay (só admin): lista os membros vinculados, numerando-os,
    e pede para o admin selecionar pelo número o jogador que deseja
    desvincular."""
    registros = carregar_registros()
    tags_vinculadas = list(registros.keys())
    if not tags_vinculadas:
        return "🔗 Nenhum membro vinculou o WhatsApp ainda. Use `!registrar #TAGJOGADOR <apelido>` para vincular."

    fluxos = carregar_fluxo_vincular()
    fluxos[chat_jid] = {
        "tipo": "desvincularplay",
        "admin": remetente_jid,
        "jogadores": tags_vinculadas,
    }
    salvar_fluxo_vincular(fluxos)

    linhas = []
    for indice, tag in enumerate(tags_vinculadas, start=1):
        info = registros[tag]
        nick = info.get("nome_coc", "Desconhecido")
        nome_clan = info.get("nome_clan")
        clan_txt = f" — {nome_clan}" if nome_clan else ""
        linhas.append(f"{indice}. {nick} — {tag}{clan_txt}")
    texto_lista = "\n".join(linhas)

    return (
        "🗑️ *DESVINCULAR PLAYER*\n\n"
        f"{texto_lista}\n\n"
        "Selecione o player que deseja desvincular (envie o *número* correspondente).\n\n"
        "💡 Digite `!vinculados` para ver esta lista sem precisar excluir."
    )


def processar_fluxo_desvincularcla(mensagem_texto, chat_jid, remetente_jid):
    """Processa a resposta numérica do !desvincularcla e exclui o clã
    escolhido."""
    fluxos = carregar_fluxo_vincular()
    fluxo = fluxos.get(chat_jid)
    if not fluxo or fluxo.get("tipo") != "desvincularcla":
        return None
    if fluxo.get("admin") != remetente_jid:
        return None

    texto_limpo = (mensagem_texto or "").strip()
    if not texto_limpo.isdigit():
        return "⚠️ Responda apenas com o *número* do clã que deseja excluir (ex: `1`)."

    indice = int(texto_limpo)
    clas = fluxo.get("clas", [])
    if indice < 1 or indice > len(clas):
        return f"⚠️ Número inválido. Envie um número de *1 a {len(clas)}*."

    tag = clas[indice - 1]

    grupos = carregar_grupos_clas()
    clas_grupo = grupos.get(chat_jid, {})
    if tag not in clas_grupo:
        del fluxos[chat_jid]
        salvar_fluxo_vincular(fluxos)
        return f"❌ O clã ({tag}) já não está mais cadastrado neste grupo. Use `!vinculados` para listar os atuais."

    nome = clas_grupo[tag].get("nome") or tag
    del clas_grupo[tag]
    grupos[chat_jid] = clas_grupo
    salvar_grupos_clas(grupos)

    del fluxos[chat_jid]
    salvar_fluxo_vincular(fluxos)

    return f"✅ Clã *{nome}* ({tag}) desvinculado deste grupo."


def processar_fluxo_desvincularplay(mensagem, chat_jid, remetente_jid):
    """Processa a resposta numérica do !desvincularplay e desvincula o player
    escolhido."""
    fluxos = carregar_fluxo_vincular()
    fluxo = fluxos.get(chat_jid)
    if not fluxo or fluxo.get("tipo") != "desvincularplay":
        return None
    if fluxo.get("admin") != remetente_jid:
        return None

    texto_limpo = (mensagem or "").strip()
    if not texto_limpo.isdigit():
        return "⚠️ Responda apenas com o *número* do player que deseja desvincular (ex: `1`)."

    indice = int(texto_limpo)
    jogadores = fluxo.get("jogadores", [])
    if indice < 1 or indice > len(jogadores):
        return f"⚠️ Número inválido. Envie um número de *1 a {len(jogadores)}*."

    tag = jogadores[indice - 1]

    registros = carregar_registros()
    if tag not in registros:
        del fluxos[chat_jid]
        salvar_fluxo_vincular(fluxos)
        return f"❌ O player ({tag}) já não está mais vinculado. Use `!vinculados` para ver os atuais."

    nome = registros[tag].get("nome_coc") or tag
    del registros[tag]
    salvar_registros(registros)

    del fluxos[chat_jid]
    salvar_fluxo_vincular(fluxos)

    return f"✅ O player *{nome}* ({tag}) foi desvinculado."


# ==========================================
# 5. FLUXO DE VENDAS / ONBOARDING (CHAT PRIVADO)
# ==========================================
# ATENÇÃO: ajuste os valores dos planos abaixo antes de colocar em produção.
TEXTO_SERVICOS = (
    "🤖 *BOT CLASH OF CLANS — MONITORAMENTO DE GUERRAS*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "O que o bot faz pelo seu clã:\n"
    "• Avisa quando a guerra começa e quando está terminando\n"
    "• Mostra quem já atacou e quem ainda falta atacar\n"
    "• Relatório automático de guerra a cada 6 horas\n"
    "• Estatísticas mensais de estrelas de guerra\n"
    "• Avisos de Raide da Capital e Jogos do Clã\n"
    "• Consulta de dados de jogadores e do clã\n\n"
    "Escolha uma opção:\n"
    "*1* — Contratar o serviço\n"
    "*2* — Falar com o administrador"
)

TEXTO_PLANOS = (
    "💳 *PLANO*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "📅 Mensal — R$ 30,00\n"
    "🎁 *Inclui 2 semanas grátis de teste!*\n\n"
    f"🔑 *Chave PIX:* {PIX_CHAVE}\n\n"
    "Após o pagamento, envie o *comprovante* (foto ou arquivo) aqui mesmo."
)

TEXTO_PAINEL_ADMIN = (
    "🛠️ *PAINEL ADMINISTRATIVO*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "• !vincularcla #TAGDOCLA <apelido> — cadastra um novo clã no seu grupo\n"
    "• !avisosguerraon#TAG — ATIVA os avisos automáticos (guerra/CWL de 4/4h, raide no fim, 1h antes/depois, jogos)\n"
    "• !avisosguerraoff#TAG — DESATIVA os avisos automáticos\n"
)


def processar_fluxo_vendas(mensagem_texto, remetente_jid):
    """Conduz a conversa de vendas + onboarding com um comprador no chat
    privado, com o estado salvo em ARQUIVO_VENDAS por número do comprador."""
    vendas = carregar_vendas()
    venda = vendas.get(remetente_jid)
    texto_limpo = (mensagem_texto or "").strip()

    # Primeiro contato: mostra a lista de serviços e o menu inicial
    if venda is None:
        vendas[remetente_jid] = {"estado": "menu", "dados": {}}
        salvar_vendas(vendas)
        return TEXTO_SERVICOS

    estado = venda.get("estado")

    if estado == "menu":
        if texto_limpo == "1":
            venda["estado"] = "aguardando_comprovante"
            vendas[remetente_jid] = venda
            salvar_vendas(vendas)
            return TEXTO_PLANOS
        if texto_limpo == "2":
            if ADMIN_NUMERO_PESSOAL:
                enviar_whatsapp(
                    f"📞 *Novo contato!* O número {remetente_jid.split('@')[0]} quer falar com você.",
                    ADMIN_NUMERO_PESSOAL
                )
            return "✅ Certo! Avisei o administrador, ele vai te chamar por aqui em breve."
        return TEXTO_SERVICOS

    if estado == "aguardando_comprovante":
        venda["estado"] = "aguardando_confirmacao"
        vendas[remetente_jid] = venda
        salvar_vendas(vendas)
        # Notifica o administrador (número pessoal do .env) de que há um
        # pagamento aguardando confirmação — POST explícito para a Evolution
        # API via enviar_whatsapp.
        if ADMIN_NUMERO_PESSOAL:
            enviar_whatsapp(
                "💰 *Pagamento para confirmar!*\n\n"
                f"Cliente: {remetente_jid.split('@')[0]}\n"
                "Enviou o comprovante e está aguardando sua confirmação.\n\n"
                "Digite apenas o número da opção:\n"
                "*1* — ✅ Confirmar pagamento\n"
                "*2* — ❌ Recusar pagamento\n\n"
                f"Ou use `!confirmarpagamento#{remetente_jid.split('@')[0]}` (liberar) "
                f"e `!recusarpagamento#{remetente_jid.split('@')[0]}` (recusar).",
                ADMIN_NUMERO_PESSOAL
            )
        return "Aguarde, o administrador já irá confirmar e prosseguir com a configuração em seu grupo."

    if estado == "aguardando_confirmacao":
        return "⏳ Aguardando a confirmação do administrador. Assim que o pagamento for confirmado, eu falo com você novamente por aqui."

    if estado == "cadastro_tag_cla":
        tag = normalizar_tag(texto_limpo)
        resultado, dados_tag = validar_tag_no_onboarding(tag, "cla") if tag else ("nao_existe", None)
        if resultado == "nao_existe":
            return "❌ Não encontrei esse clã no Clash of Clans. Envie novamente a *tag do clã* (ex: #ABC123)."
        if resultado == "tipo_errado":
            nome_outro = dados_tag.get("name") if dados_tag else ""
            return f"⚠️ Essa tag é de um *jogador*{f' ({nome_outro})' if nome_outro else ''}, não de um clã. Envie a *tag do clã* (ex: #ABC123)."
        if resultado == "erro_api" or not dados_tag:
            return "⚠️ A API do Clash of Clans não respondeu agora. Tente novamente em instantes."
        tag = dados_tag.get("tag") or tag
        venda["dados"]["tag_cla"] = tag
        venda["dados"]["nome_cla"] = dados_tag.get("name")
        venda["estado"] = "cadastro_tag_lider"
        vendas[remetente_jid] = venda
        salvar_vendas(vendas)
        return f"✅ Clã *{dados_tag.get('name')}* encontrado!\n\nAgora me envie a *tag do líder do clã*."

    if estado == "cadastro_tag_lider":
        tag_lider = normalizar_tag(texto_limpo)
        resultado, dados_tag = validar_tag_no_onboarding(tag_lider, "jogador") if tag_lider else ("nao_existe", None)
        if resultado == "nao_existe":
            return "❌ Não encontrei esse jogador no Clash of Clans. Envie novamente a *tag do líder* (ex: #ABC123)."
        if resultado == "tipo_errado":
            nome_outro = dados_tag.get("name") if dados_tag else ""
            return f"⚠️ Essa tag é de um *clã*{f' ({nome_outro})' if nome_outro else ''}, não de um jogador. Envie a *tag do líder* (ex: #ABC123)."
        if resultado == "erro_api":
            return "⚠️ A API do Clash of Clans não respondeu agora. Tente novamente em instantes."
        venda["dados"]["tag_lider"] = dados_tag.get("tag") or tag_lider
        venda["estado"] = "cadastro_numero_admin"
        vendas[remetente_jid] = venda
        salvar_vendas(vendas)
        return (
            "✅ Tag do líder salva!\n\n"
            "Agora me envie o *número de WhatsApp do administrador do clã* "
            "(com DDD/DDI, ex: 5511999999999).\n\n"
            "⚠️ Esse número será o único com acesso aos comandos administrativos do bot."
        )

    if estado == "cadastro_numero_admin":
        numero_jid = normalizar_numero_para_jid(texto_limpo)
        if not numero_jid:
            return "⚠️ Número inválido. Envie apenas os números com DDD/DDI (ex: 5511999999999)."
        venda["dados"]["numero_admin"] = numero_jid
        venda["estado"] = "cadastro_link_grupo"
        vendas[remetente_jid] = venda
        salvar_vendas(vendas)
        return "✅ Número salvo!\n\nAgora me envie o *link do grupo do WhatsApp* (https://chat.whatsapp.com/...)."

    if estado == "cadastro_link_grupo":
        dados_cadastro = venda["dados"]
        dados_cadastro["link_grupo"] = texto_limpo

        # Guarda o número do cliente como "futuro admin" da tag do clã dele.
        # Assim que alguém rodar !vincularcla com essa tag
        # dentro do grupo, esse número é reconhecido automaticamente como
        # admin cadastrado do grupo — sem precisar de comando manual.
        registrar_admin_pendente(dados_cadastro.get("tag_cla"), dados_cadastro.get("numero_admin"))

        # O bot NÃO entra mais sozinho nos grupos: o administrador adiciona o
        # número do bot manualmente e conclui o cadastro do clã dentro do
        # grupo usando o comando !vincularcla.
        if ADMIN_NUMERO_PESSOAL:
            enviar_whatsapp(
                "📋 *Cadastro pronto para configurar (entrada manual):*\n\n"
                f"Clã: {dados_cadastro.get('nome_cla')} ({dados_cadastro.get('tag_cla')})\n"
                f"Líder: {dados_cadastro.get('tag_lider')}\n"
                f"Admin do grupo: {dados_cadastro.get('numero_admin', '').split('@')[0]}\n"
                f"Link do grupo: {dados_cadastro.get('link_grupo')}\n\n"
                "Adicione o número do bot no grupo e use `!vincularcla #TAGDOCLA <apelido>` "
                "dentro dele para cadastrar o clã.",
                ADMIN_NUMERO_PESSOAL
            )

        del vendas[remetente_jid]
        salvar_vendas(vendas)

        return (
            "🎉 *Cadastro concluído!* 🤖\n\n"
            "O administrador vai adicionar o bot no seu grupo e concluir a "
            "configuração por lá. Em breve o grupo estará monitorado!"
        )

    # Estado desconhecido/corrompido: reinicia o funil
    vendas[remetente_jid] = {"estado": "menu", "dados": {}}
    salvar_vendas(vendas)
    return TEXTO_SERVICOS


def _extrair_numero_apos_prefixo(texto, prefixo):
    resto = texto[len(prefixo):].lstrip("#:").strip()
    return normalizar_numero_para_jid(resto)


def _vendas_aguardando_confirmacao(vendas):
    """Lista os JIDs de clientes com pagamento aguardando confirmação do
    administrador (estado 'aguardando_confirmacao')."""
    return [
        jid
        for jid, v in vendas.items()
        if v.get("estado") == "aguardando_confirmacao"
    ]


def processar_admin_privado(mensagem_texto, remetente_jid):
    """Comandos que só o SEU número pessoal pode usar, no chat privado com
    o bot: confirmar/recusar pagamento de um cliente em onboarding."""
    if not mensagem_texto:
        return None
    texto_lower = mensagem_texto.strip().lower()

    # Atalho por número: "1" confirma e "2" recusa o pagamento pendente.
    # Se houver mais de um pagamento aguardando, pede o comando completo.
    if texto_lower in ("1", "2"):
        vendas = carregar_vendas()
        pendentes = _vendas_aguardando_confirmacao(vendas)
        if len(pendentes) == 1:
            numero_jid = pendentes[0]
            if texto_lower == "1":
                vendas[numero_jid] = {"estado": "cadastro_tag_cla", "dados": {}}
                salvar_vendas(vendas)
                enviar_whatsapp(
                    "✅ Pagamento confirmado! Vamos iniciar seu mini cadastro.\n\nMe envie a *tag do seu clã* (ex: #ABC123).",
                    numero_jid
                )
                return f"✅ Pagamento de {numero_jid.split('@')[0]} confirmado. Iniciei o cadastro com o cliente."
            del vendas[numero_jid]
            salvar_vendas(vendas)
            enviar_whatsapp(
                "❌ Não conseguimos confirmar seu pagamento. Se foi um engano, envie o comprovante novamente ou peça para falar com o administrador.",
                numero_jid
            )
            return f"❌ Pagamento de {numero_jid.split('@')[0]} recusado. Cliente avisado."
        if len(pendentes) > 1:
            numeros = " / ".join(j.split("@")[0] for j in pendentes)
            return (
                f"⚠️ Há *{len(pendentes)}* pagamentos aguardando ({numeros}). "
                "Use `!confirmarpagamento#NUMERO` ou `!recusarpagamento#NUMERO` para escolher."
            )
        return "❌ Nenhum pagamento aguardando confirmação no momento."

    if texto_lower.startswith("!confirmarpagamento"):
        numero_jid = _extrair_numero_apos_prefixo(mensagem_texto, mensagem_texto[:len("!confirmarpagamento")])
        if not numero_jid:
            return "⚠️ Use o formato: `!confirmarpagamento#NUMERO`"
        vendas = carregar_vendas()
        if numero_jid not in vendas:
            return f"❌ Não encontrei nenhuma venda pendente para {numero_jid.split('@')[0]}."
        vendas[numero_jid] = {"estado": "cadastro_tag_cla", "dados": {}}
        salvar_vendas(vendas)
        enviar_whatsapp(
            "✅ Pagamento confirmado! Vamos iniciar seu mini cadastro.\n\nMe envie a *tag do seu clã* (ex: #ABC123).",
            numero_jid
        )
        return f"✅ Pagamento de {numero_jid.split('@')[0]} confirmado. Iniciei o cadastro com o cliente."

    if texto_lower.startswith("!recusarpagamento"):
        numero_jid = _extrair_numero_apos_prefixo(mensagem_texto, mensagem_texto[:len("!recusarpagamento")])
        if not numero_jid:
            return "⚠️ Use o formato: `!recusarpagamento#NUMERO`"
        vendas = carregar_vendas()
        if numero_jid in vendas:
            del vendas[numero_jid]
            salvar_vendas(vendas)
        enviar_whatsapp(
            "❌ Não conseguimos confirmar seu pagamento. Se foi um engano, envie o comprovante novamente ou peça para falar com o administrador.",
            numero_jid
        )
        return f"❌ Pagamento de {numero_jid.split('@')[0]} recusado. Cliente avisado."

    return None


def processar_painel_admin_privado(mensagem_texto, remetente_jid, grupo_jid):
    """Painel exclusivo do administrador cadastrado de UM clã/grupo,
    acessado pelo chat privado com o bot."""
    texto_limpo = (mensagem_texto or "").strip()
    texto_lower = _sem_acentos(texto_limpo).lower()

    if texto_lower.startswith("!vincularcla"):
        resto = texto_limpo[len("!vincularcla"):]
        if resto and not resto[0].isspace():
            return "⚠️ Use um espaço após o comando. Ex: `!vincularcla #TAGDOCLA <apelido>`"
        resto = resto.strip().lstrip(":").strip()
        return comando_vincularcla(resto, grupo_jid)
    if texto_lower.startswith("!avisosguerraoff"):
        tag = _extrair_tag_resolvido(texto_limpo, texto_limpo[:len("!avisosguerraoff")], grupo_jid)
        return comando_avisosguerraoff(tag, grupo_jid)
    if texto_lower.startswith("!avisosguerraon"):
        tag = _extrair_tag_resolvido(texto_limpo, texto_limpo[:len("!avisosguerraon")], grupo_jid)
        return comando_avisosguerraon(tag, grupo_jid)

    # Comandos gerais do bot também valem para o admin no chat privado,
    # respondendo direto no chat de origem (o próprio remetente).
    if texto_limpo.startswith("!"):
        # Chegou até aqui só porque remetente_jid já foi confirmado como o
        # admin cadastrado de grupo_jid (ver obter_grupo_do_admin no
        # webhook) — por isso já é tratado como autorizado.
        resposta = processar_comando_registro(texto_limpo, remetente_jid, chat_jid=remetente_jid, admin_ja_autorizado=True)
        if resposta is None:
            resposta = processar_comandos_gerais(texto_limpo, remetente_jid, remetente_jid=remetente_jid, admin_ja_autorizado=True)
        if resposta:
            return resposta
        return _aviso_comando_nao_reconhecido(texto_limpo)

    return TEXTO_PAINEL_ADMIN


# ==========================================
# AVISO DE COMANDO NÃO RECONHECIDO
# ==========================================
_COMANDOS_CONHECIDOS = (
    "!registrar", "!perfil", "!perfilcla", "!cla", "!clans", "!membros",
    "!cvs", "!guerra", "!historico", "!atacar", "!status", "!capital",
    "!doacoes", "!doacoestemporadapassada", "!trofeus", "!troféus",
    "!comandos", "!comandosadm", "!detalhes", "!ajuda", "!vinculados",
    "!vincularcla", "!desvincularcla", "!desvincularplay",
    "!avisosguerraon", "!avisosguerraoff",
)

# Comandos antigos/removidos → sugestão do comando atual equivalente
_COMANDOS_SUBSTITUIDOS = {
    "!cwl": "!status",
    "!estatistica": "!status",
    "!estatística": "!status",
    "!historicoguerra": "!historico",
    "!ataquesfeitos": "!atacar",
    "!ataques": "!atacar",
    "!faltamatacar": "!atacar",
    "!aviso": "!atacar",
    "!composicao": "!cvs",
    "!totaldecv": "!cvs",
    "!registrarcla": "!vincularcla",
    "!excluircla": "!desvincularcla",
    "!excluir": "!desvincularcla",
    "!guerraon": "!avisosguerraon",
    "!guerraoff": "!avisosguerraoff",
    "!avisocwlon": "!avisosguerraon",
    "!avisocwloff": "!avisosguerraoff",
    "!avisoscwlon": "!avisosguerraon",
    "!avisoscwloff": "!avisosguerraoff",
    "!detalhesguerra": "!status",
    "!detalhescwl": "!status",
    "!jogador": "!perfil",
    "!marcarvinculados": "!atacar",
}


def _sem_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def _aviso_comando_nao_reconhecido(texto):
    """Quando um comando '!...' não foi reconhecido pelo bot, explica o motivo
    com educação: acento no nome do comando, tag colada sem espaço ou comando
    inexistente. Retorna None se a mensagem não for um comando ('!')."""
    texto = (texto or "").strip()
    if not texto.startswith("!"):
        return None

    primeira = (texto.split() or [""])[0]
    base = primeira.split("#")[0].lower()
    tem_tag_colada = "#" in primeira

    if base in _COMANDOS_CONHECIDOS:
        return None

    base_sem_acento = _sem_acentos(base)
    if base_sem_acento in _COMANDOS_CONHECIDOS:
        correto = next(c for c in _COMANDOS_CONHECIDOS if c == base_sem_acento)
        texto_aviso = (
            f"⚠️ Comando com acento! Você digitou *{primeira}*, mas o "
            f"comando correto é `{correto}` (sem acento)."
        )
    elif base_sem_acento in _COMANDOS_SUBSTITUIDOS:
        novo = _COMANDOS_SUBSTITUIDOS[base_sem_acento]
        texto_aviso = (
            f"⚠️ O comando *{primeira}* não existe mais. Use `{novo}` no lugar."
        )
    else:
        texto_aviso = (
            "❌ Comando não reconhecido. Digite `!comandos` para ver a lista "
            "de comandos disponíveis (ex: `!cla meucla`, `!perfil`, `!status`)."
        )

    if tem_tag_colada:
        texto_aviso += " Lembre-se de usar um espaço após o comando."
    return texto_aviso


@app.route('/webhook', methods=['POST'])
def webhook_whatsapp():
    # get_json(silent=True) nunca levanta exceção: se o corpo vier vazio ou
    # não for JSON válido, devolve None em vez de estourar um 400/500 antes
    # mesmo de entrar no try/except abaixo.
    dados = request.get_json(silent=True)
    try:
        dados = dados or {}
        # Uso de "(dados.get(x) or {})" em vez de "dados.get(x, {})":
        # o valor default só entra quando a CHAVE não existe. Se a Evolution
        # API mandar a chave presente com valor None (comum em campos
        # opcionais), "dados.get(x, {})" devolveria None mesmo assim e o
        # próximo ".get(...)" encadeado quebraria com AttributeError.
        dados_data = dados.get("data") or {}
        dados_mensagem = dados_data.get("message") or {}
        extended_text = dados_mensagem.get("extendedTextMessage") or {}
        mensagem_texto = (
            dados_mensagem.get("conversation")
            or extended_text.get("text")
        )
        key_info = dados_data.get("key") or {}
        remoteJid = key_info.get("remoteJid")
        # Em mensagens de grupo, quem enviou de fato vem em "participant";
        # remoteJid nesse caso é o JID do próprio grupo (...@g.us)
        remetente_jid = key_info.get("participant") or remoteJid

        print(f"Mensagem recebida: {mensagem_texto!r} | remetente: {remetente_jid}")

        if not remetente_jid:
            return jsonify({"status": "recebido"}), 200

        is_grupo = bool(remoteJid and remoteJid.endswith("@g.us"))
        # Chat de origem: o grupo (...@g.us) ou o próprio remetente no chat
        # privado (...@s.whatsapp.net) — a resposta vai sempre para cá.
        chat_origem = remoteJid or remetente_jid

        # --- Mensagem é um comando do bot (começa com "!") ---
        if mensagem_texto and mensagem_texto.strip().startswith("!"):
            # Enquanto houver um fluxo de vínculo/desvínculo ativo neste grupo,
            # comandos de quem NÃO for o admin que iniciou são ignorados.
            if is_grupo and chat_origem in carregar_fluxo_vincular():
                fluxo_ativo = carregar_fluxo_vincular().get(chat_origem)
                if fluxo_ativo and fluxo_ativo.get("admin") != remetente_jid:
                    return jsonify({"status": "recebido"}), 200

            # Grupo sem clã vinculado: o único comando liberado é o
            # !vincularcla (cadastro do primeiro clã). Ao concluir o vínculo,
            # o próprio bot envia as instruções de como acionar os comandos
            # do admin e os comandos livres para todos.
            if is_grupo and not grupo_esta_registrado(chat_origem):
                comando_lower = mensagem_texto.strip().lower()
                if not comando_lower.startswith("!vincularcla"):
                    enviar_whatsapp(
                        "🔒 *Ative os comandos do grupo primeiro!*\n\n"
                        "Este grupo ainda não tem nenhum clã vinculado. "
                        "Um administrador precisa digitar:\n\n"
                        "`!vincularcla #TAGDOCLA <apelido>`\n\n"
                        "para cadastrar o primeiro clã e liberar todos os comandos aqui.",
                        chat_origem,
                    )
                    return jsonify({"status": "recebido"}), 200

            # 1) Chat privado do dono: comandos de confirmação de pagamento
            if (not is_grupo
                    and ADMIN_NUMERO_PESSOAL
                    and remetente_jid == ADMIN_NUMERO_PESSOAL):
                resposta = processar_admin_privado(mensagem_texto, remetente_jid)
                if resposta:
                    enviar_whatsapp(resposta, remetente_jid)
                return jsonify({"status": "recebido"}), 200

            # 2) Chat privado do admin exclusivo de algum grupo: painel
            if not is_grupo:
                grupo_do_admin = obter_grupo_do_admin(remetente_jid)
                if grupo_do_admin:
                    resposta = processar_painel_admin_privado(mensagem_texto, remetente_jid, grupo_do_admin)
                    if resposta:
                        enviar_whatsapp(resposta, remetente_jid)
                    return jsonify({"status": "recebido"}), 200

            # 3) Qualquer outro comando (em grupo ou chat privado): processa e
            # responde direto no chat de origem de quem enviou. Se nada for
            # reconhecido, manda um aviso educado (acento, tag colada sem
            # espaço ou comando inexistente).
            resposta = processar_comando_registro(mensagem_texto, remetente_jid, chat_jid=chat_origem)
            if resposta is None:
                resposta = processar_comandos_gerais(mensagem_texto, chat_origem, remetente_jid=remetente_jid)
            if resposta:
                enviar_whatsapp(resposta, chat_origem)
            else:
                aviso = _aviso_comando_nao_reconhecido(mensagem_texto)
                if aviso:
                    enviar_whatsapp(aviso, chat_origem)
            return jsonify({"status": "recebido"}), 200

        # --- Mensagem NÃO é comando ---

        if is_grupo:
            # Fluxo interativo de !desvincularcla / !desvincularplay
            # (respostas do admin)
            resposta_fluxo = processar_fluxo_desvincularcla(mensagem_texto, chat_origem, remetente_jid)
            if resposta_fluxo is None:
                resposta_fluxo = processar_fluxo_desvincularplay(mensagem_texto, chat_origem, remetente_jid)
            if resposta_fluxo:
                enviar_whatsapp(resposta_fluxo, chat_origem)
                return jsonify({"status": "recebido"}), 200

            # Enquanto houver um fluxo de desvínculo ativo no grupo, o
            # bot só responde a quem o iniciou — mensagens de qualquer outra
            # pessoa são ignoradas por completo (inclusive o TEXTO_SERVICOS),
            # para ninguém interferir até ele ser concluído.
            if chat_origem in carregar_fluxo_vincular():
                return jsonify({"status": "recebido"}), 200

            # Grupo sem clã cadastrado: resposta automática de vendas imediata
            # no grupo. Grupos já cadastrados respondem apenas aos comandos.
            if not grupo_esta_registrado(chat_origem):
                enviar_whatsapp(TEXTO_SERVICOS, chat_origem)
            return jsonify({"status": "recebido"}), 200

        # Chat privado do dono: atalhos numéricos de confirmação ("1"/"2")
        if (not is_grupo
                and ADMIN_NUMERO_PESSOAL
                and remetente_jid == ADMIN_NUMERO_PESSOAL
                and mensagem_texto
                and mensagem_texto.strip() in ("1", "2")):
            resposta = processar_admin_privado(mensagem_texto, remetente_jid)
            if resposta:
                enviar_whatsapp(resposta, remetente_jid)
            return jsonify({"status": "recebido"}), 200

        # Chat privado: funil de vendas/onboarding — qualquer mensagem inicia a
        # automação (menu, plano, comprovante...). Chamado mesmo sem texto para
        # cobrir o envio do comprovante como imagem/documento sem legenda.
        resposta = processar_fluxo_vendas(mensagem_texto, remetente_jid)
        if resposta:
            enviar_whatsapp(resposta, remetente_jid)

    except Exception as e:
        print(f"Erro ao processar webhook: {e}")

    return jsonify({"status": "recebido"}), 200


def _loop_automatico():
    """Loop de fundo: guerra, raide, CWL, doações, troféus, jogos do clã...
    Roda pra sempre numa thread separada, verificando tudo a cada 30s."""
    ultimo_envio_guerra = {}  # (grupo, tag) -> timestamp do último relatório de 4 em 4 horas
    ultimo_envio_cwl = {}  # (grupo, tag) -> timestamp do último aviso periódico de CWL
    ultimo_estado_guerra = {}  # (grupo, tag) -> fingerprint (estado dos ataques) do último relatório

    while True:
        try:
            pares = obter_pares_grupo_cla()

            for grupo, tag in pares:
                loop_raides(grupo, tag)
                loop_guerra(grupo, tag)
                loop_avisos_fixos_cwl(grupo, tag)
                loop_bonus_liga(grupo, tag)
                atualizar_totais_estrelas(tag)
                relatorio_estrelas_mensais(grupo, tag)
                atualizar_doacoes(tag)
                loop_relatorio_trofeus(grupo, tag)

            # Raide diária e Jogos do Clã: avisos AGRUPADOS POR GRUPO. Quando
            # o grupo tem mais de um clã, sai UMA mensagem só (lista única,
            # sem identificar de qual clã é cada jogador) para o grupo não
            # receber uma mensagem para cada clã vinculado.
            tags_por_grupo = {}
            for grupo, tag in pares:
                tags_por_grupo.setdefault(grupo, []).append(tag)
            for grupo, tags in tags_por_grupo.items():
                loop_aviso_raide_diario(grupo, tags)
                loop_jogos_do_cla(grupo, tags)

            # Relatório de guerra periódico (4 em 4 horas), respeitando
            # !avisosguerraoff/!avisosguerraon por clã em cada grupo
            # Chave (grupo, tag), não só tag: se o mesmo clã estiver
            # cadastrado em mais de um grupo, cada grupo tem seu próprio
            # relógio — senão o primeiro grupo a disparar "resetava" o
            # timer também para os outros grupos com o mesmo clã.
            tempo_atual = time.time()
            for grupo, tag in pares:
                if not guerra_ligada_para_clan(grupo, tag):
                    continue
                chave = (grupo, tag)
                if tempo_atual - ultimo_envio_guerra.get(chave, 0) >= 14400:
                    msg_guerra, pendentes, jids, fingerprint = relatorio_guerra(tag)
                    if msg_guerra:
                        if ultimo_estado_guerra.get(chave) == fingerprint:
                            enviar_whatsapp(f"🏛️ Clã: *{_nome_oficial_clan(tag, grupo)}*\n\n⚠️ *Ninguém atacou desde o último relatório.*", grupo)
                        else:
                            enviar_whatsapp(msg_guerra, grupo, mencionados=jids or None)
                            ultimo_estado_guerra[chave] = fingerprint
                    ultimo_envio_guerra[chave] = tempo_atual

            # Aviso periódico de CWL (4 em 4 horas), respeitando
            # !avisosguerraoff/!avisosguerraon por clã em cada grupo
            for grupo, tag in pares:
                chave = (grupo, tag)
                if tempo_atual - ultimo_envio_cwl.get(chave, 0) >= 14400:
                    loop_aviso_periodico_cwl(grupo, tag)
                    ultimo_envio_cwl[chave] = tempo_atual

        except Exception as e:
            print(f"Erro no ciclo: {e}")

        time.sleep(30)


_ciclo_ja_iniciado = False
_lock_ciclo_automatico = threading.Lock()


def iniciar_ciclos_automaticos():
    """Inicia a thread do loop automático (guerra, raide, CWL...) uma única
    vez por processo. É chamada logo abaixo, na importação do módulo — não
    só dentro de `if __name__ == "__main__"` — porque em produção o
    Gunicorn IMPORTA main.py como módulo (não executa como script), então
    o bloco `__main__` nunca rodaria e o loop de avisos nunca começaria.

    ⚠️ ATENÇÃO se for rodar com Gunicorn: mantenha --workers 1. Cada worker
    do Gunicorn é um PROCESSO separado (não thread) — se houver mais de um
    worker, cada um importa este módulo e inicia o SEU PRÓPRIO loop,
    duplicando (ou triplicando...) os relatórios e avisos automáticos
    enviados no grupo. O estado do bot (arquivos JSON + threads em memória)
    não foi feito para coordenar múltiplos processos."""
    global _ciclo_ja_iniciado
    with _lock_ciclo_automatico:
        if _ciclo_ja_iniciado:
            return
        _ciclo_ja_iniciado = True

    print("🤖 Robô do Clash of Clans iniciado — loop automático de guerra/raide/CWL ativo...")
    inicializar_grupo_padrao()
    t_ciclo = threading.Thread(target=_loop_automatico, daemon=True)
    t_ciclo.start()


# Inicia o loop automático assim que o módulo é carregado — funciona tanto
# rodando `python main.py` (dev) quanto sendo importado pelo Gunicorn
# (produção, ver Dockerfile: `gunicorn ... main:app`).
iniciar_ciclos_automaticos()


# ==========================================
# EXECUÇÃO PRINCIPAL (modo dev / sem Gunicorn)
# ==========================================
if __name__ == "__main__":
    # Em produção use o Gunicorn (ver Dockerfile) em vez de rodar este
    # arquivo diretamente: o servidor embutido do Flask (app.run) não é
    # feito pra produção (single-threaded/sem gestão de processos, sem
    # graceful restart, etc.). Isso aqui é só pra testar localmente.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
