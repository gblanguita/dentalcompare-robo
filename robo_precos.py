"""
robo_precos.py — DentalCompare, Etapa 7

Robô diário que:
1. Busca no Supabase todas as ofertas (offers) que já têm url_produto
   cadastrado (ou seja, sabemos exatamente qual página visitar).
2. Abre cada página com um navegador de verdade (Playwright), porque o
   preço só aparece depois que o JavaScript da loja carrega — confirmado
   testando a página de um produto da Dental Cremer (ver etapa-7 no
   projeto pra detalhes).
3. Tenta extrair o preço com algumas estratégias diferentes (na ordem):
   dado estruturado (JSON-LD / microdata) primeiro, e um regex de
   fallback só se nada disso existir.
4. Se conseguir um preço válido, atualiza `offers.preco` e
   `offers.atualizado_em` no Supabase.
5. Se não conseguir (página mudou, produto saiu de linha, bloqueio etc.),
   NÃO mexe no preço antigo — só registra no log. Isso segue a regra do
   projeto: nunca inventar ou sobrescrever com um dado que não temos
   certeza.

IMPORTANTE — status honesto: este script foi escrito e revisado, mas
ainda NÃO foi testado contra os sites reais, porque o ambiente onde ele
foi escrito não tem acesso à internet aberta (só a ferramentas de busca
controladas). Antes de colocar pra rodar todo dia, o primeiro passo é
rodar ele manualmente contra 2-3 produtos conhecidos e comparar o preço
que ele pegou com o que aparece no site de verdade — bem provável que a
extração de preço (função extrair_preco_da_pagina) precise de ajuste
depois de ver o HTML real renderizado.

Variáveis de ambiente necessárias:
  SUPABASE_URL              ex.: https://xxxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY a service role key (NUNCA a anon key — essa
                             tem permissão de escrever direto na tabela,
                             então trate como senha: só em variável de
                             ambiente/secret do servidor, nunca no código,
                             nunca no repositório/git).

Como rodar manualmente pra testar:
  pip install playwright requests
  playwright install chromium
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python3 robo_precos.py --dry-run --limite 3

--dry-run só imprime o que encontrou, sem gravar nada no banco — use
sempre isso primeiro pra conferir se está pegando o preço certo antes de
deixar ele escrever de verdade. --limite processa só as N primeiras
ofertas, útil pra um teste rápido sem esperar o catálogo inteiro.
"""

import os
import re
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Preço mínimo/máximo plausível pra material odontológico — serve de
# trava de sanidade: se o robô "achar" um preço fora disso, é sinal de
# que pegou o número errado da página (ex.: um CEP, um código de barras,
# o preço de outro produto na tela), e por segurança a gente ignora em
# vez de gravar algo absurdo no banco.
PRECO_MIN_PLAUSIVEL = 1.0
PRECO_MAX_PLAUSIVEL = 5000.0

# Entre uma página e outra, espera esse tanto — pra não sobrecarregar o
# site da loja nem parecer um ataque. Nada aqui precisa ser rápido: é só
# 1x por dia.
PAUSA_ENTRE_PAGINAS_SEGUNDOS = 3

USER_AGENT = (
    "Mozilla/5.0 (compatible; DentalCompareBot/1.0; "
    "+https://dentalcompare-nu.vercel.app/) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def buscar_ofertas_com_url():
    """Pega no Supabase todas as ofertas que já têm url_produto
    cadastrado — essas são as que o robô sabe onde ir conferir."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/offers",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        params={
            "select": "id,product_id,store_id,preco,url_produto",
            "url_produto": "not.is.null",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def atualizar_oferta(offer_id, preco):
    """Grava o novo preço + timestamp de quando foi conferido. Só chamado
    quando a gente tem um preço plausível — nunca escreve null nem um
    valor fora da faixa de sanidade."""
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/offers",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        params={"id": f"eq.{offer_id}"},
        json={
            "preco": preco,
            "atualizado_em": datetime.now(timezone.utc).isoformat(),
        },
        timeout=30,
    )
    resp.raise_for_status()


def _parse_preco_texto(texto):
    """Acha o primeiro 'R$ 1.234,56' (ou variações) num texto e devolve
    como float. Devolve None se não achar nada com cara de preço."""
    if not texto:
        return None
    match = re.search(r"R\$\s*([\d.]{1,10},\d{2})", texto)
    if not match:
        return None
    bruto = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(bruto)
    except ValueError:
        return None


PADRAO_INDISPONIVEL = re.compile(
    r"produto indispon[íi]vel|indispon[íi]vel no momento|fora de estoque|"
    r"produto esgotado|sem estoque",
    re.IGNORECASE,
)


def extrair_preco_da_pagina(page):
    """Tenta várias formas de achar o preço na página já carregada, da
    mais confiável pra menos confiável. Retorna float ou None.

    Antes de tudo, confere se a página diz que o produto está indisponível
    — nesse caso a loja não mostra preço nenhum, e se a gente deixar o
    regex de fallback correr mesmo assim ele pode pegar o preço de outra
    coisa na tela (produto relacionado, "quem viu também viu" etc.) e
    gravar um valor errado. Então aqui a gente prefere pular e não mexer
    em nada, seguindo a regra do projeto de nunca inventar/arriscar preço.
    """
    try:
        texto_pagina = page.inner_text("body")
    except Exception:
        texto_pagina = ""

    if PADRAO_INDISPONIVEL.search(texto_pagina):
        return None

    # Estratégia 1 — dado estruturado JSON-LD (schema.org/Product). Muitas
    # lojas injetam isso via JavaScript mesmo quando o HTML inicial não
    # tem (pra SEO/Google Shopping), então mesmo sites "de app" costumam
    # ter isso depois de carregar.
    try:
        scripts = page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data = json.loads(script.inner_text())
            except (json.JSONDecodeError, ValueError):
                continue
            candidatos = data if isinstance(data, list) else [data]
            for item in candidatos:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("Product", ["Product"]):
                    continue
                oferta = item.get("offers")
                if isinstance(oferta, list):
                    oferta = oferta[0] if oferta else None
                if isinstance(oferta, dict) and oferta.get("price"):
                    try:
                        return float(str(oferta["price"]).replace(",", "."))
                    except ValueError:
                        pass
    except Exception:
        pass

    # Estratégia 2 — microdata itemprop="price" (padrão schema.org direto
    # no HTML/DOM renderizado).
    try:
        el = page.locator('[itemprop="price"]').first
        if el.count() > 0:
            valor = el.get_attribute("content") or el.inner_text()
            preco = _parse_preco_texto(valor)
            if preco:
                return preco
    except Exception:
        pass

    # Estratégia 3 — fallback: procura o primeiro "R$ 123,45" no texto
    # visível da página inteira. É o mais frágil (pode pegar o preço
    # errado se a página tiver mais de um valor em R$ visível, tipo
    # produtos relacionados) — por isso essa é a ÚLTIMA opção, e talvez
    # precise ser trocada por um seletor mais específico assim que a
    # gente ver a página de verdade renderizada.
    try:
        texto = page.inner_text("body")
        return _parse_preco_texto(texto)
    except Exception:
        return None


def processar_oferta(browser, oferta, dry_run):
    url = oferta["url_produto"]
    print(f"→ Abrindo {url}")
    page = browser.new_page()
    try:
        page.goto(url, timeout=45000, wait_until="commit")
        page.wait_for_timeout(5000)
        preco = extrair_preco_da_pagina(page)
    except PlaywrightTimeoutError:
        print("  ⚠️  Timeout carregando a página — pulando.")
        return
    finally:
        page.close()

    if preco is None:
        print(
            "  ⚠️  Não consegui achar um preço nessa página — pulando "
            "(não mexe no preço antigo)."
        )
        return

    if not (PRECO_MIN_PLAUSIVEL <= preco <= PRECO_MAX_PLAUSIVEL):
        print(
            f"  ⚠️  Preço encontrado (R$ {preco}) parece implausível — "
            f"ignorando por segurança, não vou gravar isso."
        )
        return

    preco_antigo = oferta.get("preco")
    mudou = preco_antigo is None or abs(float(preco_antigo) - preco) > 0.001
    if mudou:
        print(f"  ✓ Preço novo: R$ {preco_antigo} → R$ {preco}")
    else:
        print(
            f"  ✓ Preço igual ao que já tínhamos (R$ {preco}) — só "
            f"atualizo a data de conferência."
        )

    if dry_run:
        print("  (dry-run — não gravei nada no banco)")
    else:
        atualizar_oferta(oferta["id"], preco)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Só mostra o que encontraria, sem gravar nada no Supabase.",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Processa só as N primeiras ofertas (útil pra testar rápido).",
    )
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Faltam as variáveis SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.")
        sys.exit(1)

    ofertas = buscar_ofertas_com_url()
    if args.limite:
        ofertas = ofertas[: args.limite]

    print(f"{len(ofertas)} oferta(s) com url_produto cadastrado.")
    if not ofertas:
        print(
            "Nenhuma oferta tem url_produto ainda — esse é o próximo passo: "
            "preencher a coluna url_produto na tabela offers com o link de "
            "cada produto em cada loja, pra esse robô ter o que visitar."
        )
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for i, oferta in enumerate(ofertas, start=1):
                print(f"\n[{i}/{len(ofertas)}]", end=" ")
                processar_oferta(browser, oferta, args.dry_run)
                if i < len(ofertas):
                    time.sleep(PAUSA_ENTRE_PAGINAS_SEGUNDOS)
        finally:
            browser.close()

    print("\nConcluído.")


if __name__ == "__main__":
    main()
