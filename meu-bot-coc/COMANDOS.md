# Bot Clash of Clans — Guia de Comandos

Documento de referência de todos os comandos do bot, dos formatos aceitos,
dos níveis de acesso, das notificações automáticas e do fluxo de vendas.

---

## 1. Visão geral

- O bot responde a comandos iniciados com `!`, tanto em **grupos** quanto no
  **chat privado** com o bot.
- **Espaço obrigatório após o comando.** Todo comando com argumento usa
`!comando <argumento>` (com espaço). Formatos colados como `!perfilcla#TAG`
  **não funcionam** — o bot responde pedindo para usar o espaço.
- Os comandos aceitam o **apelido curto** cadastrado no grupo no lugar da
  tag longa. Ex.: cadastrou `!vincularcla #ABC123 meucla` → depois pode
  usar `!perfilcla meucla`, `!guerra meucla`, `!membros meucla`, etc.
- Três níveis de acesso:
  1. **Todos os membros** — consultas e vínculo de jogador no grupo.
  2. **Administrador do grupo** — comandos administrativos **dentro do
     grupo**: quem é admin/superadmin real do WhatsApp no grupo (checado na
     Evolution API), o dono do bot, ou o número que assinou o serviço
     (reconhecido automaticamente ao cadastrar o clã).
  3. **Dono do bot** — confirmação/recusa de pagamentos no **chat privado**
     (atalhos `1`/`2` e comandos `!confirmarpagamento`/`!recusarpagamento`).

> ⚠️ O **menu de vendas** só é enviado para números no **chat privado** do
> bot. Ele **não** é exibido dentro dos grupos.

---

## 2. Formato dos comandos

### Espaço obrigatório

Todo argumento vem depois de um espaço:

```
!perfilcla meucla       ✅  !perfilcla #TAGDOCLA   ✅
!perfilcla#TAGDOCLA     ❌  (sem espaço: o bot avisa)
```

O próprio bot avisa quando o formato está errado (comando com acento, tag
colada ou comando inexistente) — se baseia em uma lista interna que também
**sugere o comando atual** de comandos antigos (ver seção 10).

### Apelido de clã vs. tag completa

- **Apelido** — palavra única cadastrada no grupo com `!vincularcla
  #TAGDOCLA <apelido>`. Só vale para os clãs do próprio grupo.
- **#Tag** — sempre maiúscula, começando com `#` (ex.: `#ABC123`).

### Onde funciona cada comando

| Onde | O que funciona |
|---|---|
| **No grupo** | Todos os comandos (consulta + administração). |
| **No privado** | Painel do admin cadastrado (assinante), comandos do dono, e funil de vendas para quem ainda não assinou. |

---

## 3. Comandos para todos os membros

### Jogador (até 5 tags por número)

| Comando | O que faz |
|---|---|
| `!registrar #TAGJOGADOR <apelido ou #TAGDOCLA>` | Vincula seu WhatsApp à tag do jogador **no clã indicado** (obrigatório informar o clã depois, com espaço). Ex.: `!registrar #ABC123 meucla` ou `!registrar #ABC123 #XYZ987`. O bot valida a tag e responde com um mini-perfil do jogador. |
| `!perfil` | Perfil completo do seu vínculo (TH, liga ranqueada exata, troféus, doações, clã). Com **mais de uma conta** vinculada, mostra um resumo de todas; também aceita `!perfil #SUATAG`. |
| `!vinculados` | Lista os clãs e membros com WhatsApp vinculado (só no grupo; exige admin/dono). |

### Clã do grupo (somente clãs cadastrados no grupo)

| Comando | O que faz |
|---|---|
| `!perfilcla <apelido ou #tag>` | Card do clã em imagem (brasão, etiquetas, liga, capital, descrição). |
| `!cla <apelido ou #tag>` | Resumo do clã: nível, troféus, liga da CWL, membros, vitórias/empates/derrotas, sequência de vitórias e descrição. |
| `!membros <apelido ou #tag>` | Lista de membros com cargos abreviados e troféus. |
| `!cvs <apelido ou #tag>` | Composição de CVs — quantidade de membros por nível da Prefeitura. |
| `!clans #TAGDOCLA` | Busca de outro clã fora do grupo (qualquer tag). |

### Guerra

| Comando | O que faz |
|---|---|
| `!guerra <apelido ou #tag>` | Status da guerra em andamento — **GUERRA RANDOM** e **CWL** (placar + situação de ataques). |
| `!atacar <apelido ou #tag>` | Avisa quem **ainda não atacou** (GUERRA RANDOM + CWL): marca @número somente de quem tem WhatsApp vinculado e não atacou; quem já atacou ou não tem vínculo aparece só com o nick. |
| `!historico <apelido ou #tag>` | Histórico das últimas 10 guerras (🟢 vitória, 🟡 empate, 🔴 derrota). |
| `!status <apelido ou #tag>` | Estatísticas detalhadas da guerra/CWL em andamento — serve para a **guerra normal e para a CWL** (ver abaixo). |

### CWL (Liga de Guerra de Clãs)

| Comando | O que faz |
|---|---|
| `!status <apelido ou #tag>` | Estatísticas dos **dois** lados em paralelo: placar, destruição total, ataques usados/vencidos/perdidos/restantes, distribuição de estrelas, médias (estrelas, destruição, duração) e destaques (melhor ataque / melhor defesa) — tanto na guerra normal quanto na CWL. |
| `!guerra <apelido ou #tag>` | Relatório da rodada de CWL em andamento (também coberto pelo `!guerra` da seção acima). |

### Capital

| Comando | O que faz |
|---|---|
| `!capital <apelido ou #tag>` | Capital do clã: nível da Câmara do Clã, construções (concluídas/faltando/total), status da raide, ouro total, ataques, distritos destruídos, recompensas e ouro por atacante. |

### Doações

| Comando | O que faz |
|---|---|
| `!doacoes <apelido ou #tag>` | Ranking de doações da temporada atual (acumulado rastreado pelo bot). |
| `!doacoestemporadapassada <apelido ou #tag>` | Ranking de doações da temporada passada (só vinculados). |

### Troféus

| Comando | O que faz |
|---|---|
| `!trofeus <apelido ou #tag>` | Ranking de troféus dos membros do clã (também aceita `!troféus`). |

### Vínculo WhatsApp ↔ jogador (até 5 tags por número)

| Comando | O que faz |
|---|---|
| `!registrar #TAGJOGADOR <apelido ou #TAGDOCLA>` | Registra o vínculo do jogador com o clã informado (apelido do grupo OU #tag do clã). |
| `!perfil` | Seu perfil vinculado (resumo se tiver várias contas). |

### Ajuda

| Comando | O que faz |
|---|---|
| `!comandos` ou `!ajuda` | Tabela resumo dos comandos para todos. |
| `!detalhes` | Explicação de cada comando e do que ele mostra. |
| `!comandosadm` | Lista dos comandos exclusivos do administrador do grupo. |

---

## 4. Comandos exclusivos do administrador do grupo

> Permissões **dentro do grupo**:
> - administrador/superadmin **real** do WhatsApp no grupo (identificado
>   automaticamente pela Evolution API);
> - o dono do bot;
> - o número que assinou o serviço e foi promovido automaticamente quando o
>   clã foi cadastrado (`admins_grupo.json`).

| Comando | O que faz |
|---|---|
| `!vincularcla #TAGDOCLA <apelido>` | Cadastra um clã no grupo (limite de 5 por grupo). **O apelido é obrigatório** e deve vir com espaço após a tag. Ao cadastrar o primeiro clã, os comandos do grupo são liberados; se alguém assinou o serviço no privado para essa tag, esse número é reconhecido como admin cadastrado do grupo. |
| `!vinculados` | Lista os clãs e membros vinculados ao grupo (só admin/dono). |
| `!desvincularcla` | Lista os clãs do grupo numerados para você escolher excluir **respondendo com o número** (ex.: `1`). |
| `!desvincularplay` | Lista os membros vinculados numerados para você desvincular **respondendo com o número**. |
| `!avisosguerraon <apelido ou #tag>` | **Ativa** os avisos automáticos de GUERRA/CWL do clã (relatório de 4/4h, aviso detalhado de CWL listando quem falta atacar, alertas de 1h antes do início/fim), do bônus da liga e dos relatórios de estrelas/troféus. |
| `!avisosguerraoff <apelido ou #tag>` | **Desativa** esses avisos (a CWL passa a receber só a frase padrão sem lista de nomes). |

> 💡 **Avisos de Raide da Capital e Jogos do Clã são SEMPRE automáticos**
> (independem de `!avisosguerraon/off`). Os avisos de Guerra/CWL, bônus da
> liga, estrelas mensais e troféus só rolam depois que o admin os ativa.

---

## 5. Comandos do dono do bot (chat privado)

> Só o número pessoal configurado (`ADMIN_NUMERO_PESSOAL` no `.env`) pode usar.

| Comando | O que faz |
|---|---|
| `!confirmarpagamento#NUMERO` | Confirma o pagamento de um cliente no funil de vendas e inicia o mini-cadastro dele (perguntando a tag do clã). |
| `!recusarpagamento#NUMERO` | Recusa o pagamento e avisa o cliente na hora. |
| `1` / `2` | Atalhos numéricos: se houver **um único** pagamento pendente, `1` confirma e `2` recusa. Com mais de um pendente, o bot pede o comando completo. |

---

## 6. Painel do administrador no chat privado

O número que **assinou o serviço** (e foi promovido automaticamente quando o
clã foi cadastrado) pode usar o bot **no chat privado** para administrar o
seu grupo sem precisar estar dentro dele:

- Qualquer mensagem de texto **sem comando** no privado devolve o **painel
  administrativo** (`TEXTO_PAINEL_ADMIN`), que lista os comandos disponíveis.
- No privado valem os comandos de administração (`!vincularcla`,
  `!avisosguerraon/off`) **e todos os demais comandos**
  (`!perfilcla`, `!status`, `!doacoes`, ...) aplicados sobre o grupo dele.
- Dentro do grupo, os mesmos comandos também funcionam para quem é admin
  real do WhatsApp no grupo.

---

## 7. Fluxo automático de vendas / onboarding (chat privado)

Quando alguém que ainda **não é cliente** manda mensagem no chat privado do
bot, começa a automação de vendas:

1. **Menu inicial** — o bot apresenta o serviço (monitoramento de guerra,
   relatório de 4h, estrelas mensais, raide/jogos) e pede a opção:
   `1 — Contratar o serviço` / `2 — Falar com o administrador`.
2. **Plano** — ao escolher `1`, mostra o plano mensal (R$ 30,00 com 2
   semanas grátis) e a chave PIX; pede o **comprovante** (foto ou arquivo).
3. **Comprovante** — vai para a fila de pagamentos **aguardando
   confirmação**; o dono do bot confirma (`1`) ou recusa (`2`) no privado.
4. **Mini-cadastro** — pagamento confirmado, o bot pergunta a **tag do
   clã** e depois o **número do admin** + link do grupo.
5. **Conclusão** — o cliente é registrado como "admin pendente" daquela tag
   (`admins_pendentes.json`). Assim que o clã é cadastrado no grupo
   (`!vincularcla`), o número é promovido automaticamente a admin cadastrado
   do grupo e ganha o painel no privado.

> O número que não é cliente e digite qualquer comando no privado também
> passa pelo funil de vendas (não recebe os comandos do painel).

---

## 8. Notificações automáticas

### Quando o admin **ligou** os avisos (`!avisosguerraon`)

| Evento | Quando |
|---|---|
| ⚠️ Guerra / rodada de CWL começa em 1h | 1 hora antes do início |
| 🚨 Guerra / rodada de CWL acaba em 1h | 1 hora antes do fim |
| 📊 Relatório de guerra | A cada **4 horas** (**anti-spam**: só envia se houve mudança real de ataques; se ninguém atacou, avisa que nada mudou) |
| 📊 Aviso periódico de CWL | A cada **4 horas** — se o aviso detalhado estiver ligado, lista quem falta (marcando vinculados); senão, frase padrão |
| 🎖️ Bônus da liga | Às **13h** (horário de Brasília), quando a CWL terminou — lista quem atacou pelo menos uma vez |
| 🏆 Relatório mensal de estrelas | Na **virada do mês**, com o ranking de estrelas acumuladas |
| 🏆 Relatório de troféus | No fim da temporada (última 2ª-feira do mês, meia-noite horário do Leste) |

### Sempre automáticos (independem do `!avisos...`)

| Evento | Quando / frequência |
| --- | --- |
| 🏰 Relatório da Capital (Raide) | No **fim da raide**, com o ataque e o ouro de cada vinculado + total |
| 🏰 Lembrete diário da Raide | **sexta e sábado às 12h**; **domingo 0h/6h/12h/18h**, enquanto a raide estiver ativa — marca @número quem tem vínculo e ainda não atacou (quem atacou aparece com ✅) |
| 🎯 Jogos do Clã | **dias 22 a 28, às 12h**, um lembrete por dia (sem marcar ninguém) |

> Todos os relatórios e avisos citam o nome oficial do clã e o do oponente,
> para deixar claro a qual guerra a mensagem se refere.

---

## 9. Comandos antigos (não existem mais — o bot avisa e sugere o novo)

| Antigo | Use hoje |
| --- | --- |
| `!cwl`, `!estatistica`/`!estatística`, `!detalhescwl`, `!detalhesguerra` | `!status` |
| `!historicoguerra` | `!historico` |
| `!ataques`, `!ataquesfeitos`, `!faltamatacar`, `!aviso`, `!marcarvinculados` | `!atacar` |
| `!composicao`, `!totaldecv` | `!cvs` |
| `!jogador` | `!perfil` |
| `!registrarcla` | `!vincularcla` |
| `!excluircla`, `!excluir` | `!desvincularcla` / `!desvincularplay` |
| `!guerraon`, `!guerraoff` | `!avisosguerraon` / `!avisosguerraoff` |
| `!avisocwlon`, `!avisocwloff`, `!avisoscwlon`, `!avisoscwloff` | `!avisosguerraon` / `!avisosguerraoff` |
| `!registraadm`, `!excluir adm` | Não existem: o admin é reconhecido automaticamente (admin real do grupo no WhatsApp ou assinante promovido) |

---

## 10. Formatos e limites

| Item | Detalhe |
| --- | --- |
| Espaço | **Obrigatório** após o comando e entre os argumentos |
| Apelido | Palavra única, sem espaços (ex.: `meucla`) |
| Tag | Sempre com `#`, maiúscula (ex.: `#ABC123`) |
| Clãs por grupo | `5` |
| Tags de jogador por número WhatsApp | `5` |
| Ciclo de verificação automática | a cada 30 segundos |
| Timeout da API de guerra (CWL) | `/clanwarleagues/wars/...` usa **20 s** e **2 tentativas** (outras consultas: 10 s, 1 tentativa) |

---

## 11. Persistência de dados (arquivos JSON)

Tudo é salvo em `data/` e sobrevive a reinícios (no Docker, em volume):

| Arquivo | Conteúdo |
| --- | --- |
| `grupos_clas.json` | Grupos e clãs cadastrados (apelido, `guerra_on`, `aviso_cwl_detalhado`, `avisos_auto`, nome oficial) |
| `membros_registrados.json` | Vínculos WhatsApp ↔ jogador (tag → jid, clã, nome) |
| `admins_grupo.json` | Número assinante (admin cadastrado) de cada grupo |
| `admins_pendentes.json` | Números que assinaram no privado, aguardando a tag do clã entrar no grupo |
| `fluxo_vincular.json` | Estado dos fluxos de desvínculo (`!desvincularcla`/`!desvincularplay`) |
| `estrelas_guerra_mensal.json` | Estatísticas mensais de estrelas por clã |
| `raide_status.json` | Estado das Raides da Capital (envios e lembretes) |
| `jogos_cla_status.json` | Lembretes dos Jogos do Clã por grupo |
| `cwl_status.json` | Estado da CWL (bônus da liga já anunciado) |
| `doacoes_status.json` | Doações acumuladas por temporada (atual + passada) |
| `trofeus_status.json` | Último relatório de troféus enviado por clã |
| `vendas_pendentes.json` | Estado do funil de vendas no chat privado |

---

## 12. Configuração no `.env`

| Variável | Descrição |
| --- | --- |
| `COC_TOKEN` | Token da API oficial do Clash of Clans |
| `EVOLUTION_URL` | URL da API Evolution (WhatsApp) |
| `EVOLUTION_API_KEY` | Chave de API da Evolution |
| `INSTANCE_NAME` | Nome da instância na Evolution |
| `CLAN_TAG` | Tag do clã (legado; fallback no primeiro boot) |
| `GROUP_JID` | JID do grupo (legado; fallback no primeiro boot) |
| `ADMIN_NUMERO_PESSOAL` | JID do dono do bot (ex.: `5511999999999@s.whatsapp.net`) |
| `PIX_CHAVE` | Chave PIX exibida no fluxo de vendas |
| `PORT` | Porta do servidor webhook (padrão `5000`) |