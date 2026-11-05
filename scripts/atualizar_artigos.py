#!/usr/bin/env python3
"""
Busca TODOS os posts do blog f1descatholica (via feed publico do Blogger),
cruza as tags de cada post com o dicionario de temas (dados/dicionario-tags.txt)
e gera:
  - dados/artigos.json          -> lista de artigos organizada por tema
  - dados/tags-pendentes.json   -> tags encontradas nos posts que ainda nao
                                    tem correspondencia no dicionario
  - dados/tags-silenciadas.json -> tags sem correspondencia que foram
                                    silenciadas (regras fixas ou lista manual)
  - dados/posts-sem-tema.json   -> posts que ficaram fora de qualquer tema

Este script roda automaticamente todo dia via GitHub Actions
(.github/workflows/atualizar-artigos.yml), mas tambem pode ser
rodado manualmente:  python scripts/atualizar_artigos.py
"""

import json
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
from collections import Counter
from pathlib import Path

BLOG_URL = "https://f1descatholica.blogspot.com"
FEED_URL = BLOG_URL + "/feeds/posts/default"
MAX_POR_PAGINA = 150  # limite seguro de itens por pagina do feed do Blogger
MAX_TENTATIVAS_POR_PAGINA = 3

PASTA_DADOS = Path(__file__).resolve().parent.parent / "dados"
ARQUIVO_DICIONARIO = PASTA_DADOS / "dicionario-tags.txt"
ARQUIVO_SAIDA = PASTA_DADOS / "artigos.json"
ARQUIVO_PENDENTES = PASTA_DADOS / "tags-pendentes.json"
ARQUIVO_SEM_TEMA = PASTA_DADOS / "posts-sem-tema.json"
ARQUIVO_IGNORADAS = PASTA_DADOS / "tags-ignoradas.txt"
ARQUIVO_SILENCIADAS_TXT = PASTA_DADOS / "tags-silenciadas.txt"
ARQUIVO_SILENCIADAS = PASTA_DADOS / "tags-silenciadas.json"
TAM_MIN_PALAVRA_TITULO = 4   # ignora palavras com menos letras que isso
QTD_PALAVRAS_TITULO = 5      # quantas palavras mostrar por tag pendente

# ---- Tags IGNORADAS: o post inteiro e considerado nao tematizavel ----
# Tags que comecam com estes prefixos (ja normalizadas) sao ignoradas
PREFIXOS_IGNORADOS = ("c ", "l ")
# Citacao biblica: [numeral romano] livro [numeral romano] numero(s) de 1 a 3 digitos
PADRAO_CITACAO = re.compile(
    r"^(?:[ivx]+\s+)?[a-zà-ú]{1,6}(?:\s+[ivx]+)?[\s-]+\d{1,3}(?:\s+\d{1,3})*$"
)

# ---- Tags SILENCIADAS: so a tag sai das pendentes (o post nao e escondido) ----
# Tag que comeca com ano (1 a 4 digitos + espaco), ex.: "1978 Papa Paulo VI"
PADRAO_ANO = re.compile(r"^\d{1,4}\s")
# Tags que comecam com titulo/tipo de documento (ja normalizadas)
PREFIXOS_SILENCIADOS = (
    "padre ", "bispo ", "cardeal ", "arcebispo ", "dom ", "frei ",
    "irmão ", "monsenhor ", "papa ",
    "encíclica ", "dicastério ", "congregação ",
)


def buscar_pagina(start_index):
    url = (
        f"{FEED_URL}?alt=json&max-results={MAX_POR_PAGINA}"
        f"&start-index={start_index}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS_POR_PAGINA + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo_erro = e
            print(f"[aviso] tentativa {tentativa}/{MAX_TENTATIVAS_POR_PAGINA} "
                  f"falhou para start-index={start_index}: {e}")
            if tentativa < MAX_TENTATIVAS_POR_PAGINA:
                time.sleep(2 * tentativa)  # espera crescente entre tentativas

    raise urllib.error.URLError(
        f"falha ao buscar pagina (start-index={start_index}) apos "
        f"{MAX_TENTATIVAS_POR_PAGINA} tentativas: {ultimo_erro}"
    )


def extrair_total_resultados(pagina):
    # Total oficial de posts informado pelo proprio feed do Blogger.
    # Se por algum motivo o campo nao vier, usamos infinito para nao
    # travar o loop nessa checagem (a parada por pagina vazia continua
    # valendo como rede de seguranca).
    total = (
        pagina.get("feed", {})
        .get("openSearch$totalResults", {})
        .get("$t")
    )
    try:
        return int(total)
    except (TypeError, ValueError):
        return float("inf")


def extrair_posts_da_pagina(pagina):
    posts = []
    entradas = pagina.get("feed", {}).get("entry", [])
    descartados_sem_link = 0

    for entrada in entradas:
        post_id = entrada.get("id", {}).get("$t", "")
        titulo = entrada.get("title", {}).get("$t", "").strip()

        link = None
        for l in entrada.get("link", []):
            if l.get("rel") == "alternate":
                link = l.get("href")
                break
        if not link:
            # posts sem link "alternate" nao sao artigos publicados normais
            descartados_sem_link += 1
            continue

        tags = [
            c.get("term", "").strip()
            for c in entrada.get("category", [])
            if c.get("term")
        ]

        data_publicacao = entrada.get("published", {}).get("$t", "")

        posts.append({
            "id": post_id,
            "titulo": titulo,
            "url": link,
            "tags": tags,
            "data": data_publicacao,
        })

    return posts, descartados_sem_link


def buscar_todos_os_posts():
    todos = []
    ids_vistos = set()  # protecao contra duplicidade
    duplicados_ignorados = 0
    total_descartados_sem_link = 0
    start_index = 1
    total_esperado = None  # vem do feed na 1a resposta

    while True:
        pagina = buscar_pagina(start_index)

        # quantidade de entradas BRUTAS retornadas pelo Blogger nesta
        # pagina, antes de qualquer filtro (usada so para decidir o
        # avanco da paginacao, nao para o conteudo final)
        entradas_brutas = pagina.get("feed", {}).get("entry", [])

        if total_esperado is None:
            total_esperado = extrair_total_resultados(pagina)

        if not entradas_brutas:
            # fim real: pagina veio vazia
            break

        posts, descartados = extrair_posts_da_pagina(pagina)
        total_descartados_sem_link += descartados

        for post in posts:
            if post["id"] and post["id"] in ids_vistos:
                duplicados_ignorados += 1
                continue
            if post["id"]:
                ids_vistos.add(post["id"])
            todos.append(post)

        # avanca a paginacao com base na quantidade BRUTA recebida,
        # nao na quantidade filtrada (posts sem link "alternate" nao
        # podem fazer a paginacao parar cedo demais)
        start_index += len(entradas_brutas)

        if start_index > total_esperado:
            break

    if total_descartados_sem_link > 0:
        print(f"[info] {total_descartados_sem_link} posts descartados "
              f"por nao terem link 'alternate' (nao sao artigos publicados normais).")
    if duplicados_ignorados > 0:
        print(f"[info] {duplicados_ignorados} posts duplicados ignorados.")

    return todos


def normalizar_tag(tag):
    """
    Forma padrao usada em TODA comparacao de tags (dicionario, ignoradas,
    silenciadas e regras): remove emojis e caracteres invisiveis ligados a
    eles, colapsa espacos duplicados e ignora maiusculas/minusculas.
    """
    tag = unicodedata.normalize("NFC", tag)
    limpo = []
    for ch in tag:
        if ch in ("\ufe0e", "\ufe0f", "\u200d"):
            continue  # seletores de variacao e juncao de emoji
        # So = simbolos/emoji/bandeiras, Sk = modificadores (tom de pele),
        # Cf = invisiveis de formatacao, Cn/Co = sem uso definido
        if unicodedata.category(ch) in ("So", "Sk", "Cf", "Cn", "Co"):
            continue
        limpo.append(ch)
    return " ".join("".join(limpo).split()).lower()


def carregar_dicionario():
    """
    Formato do arquivo dados/dicionario-tags.txt, uma linha por tag:

        tag exata do blogger => id_tema1, id_tema2

    - Linhas vazias ou comecando com # sao ignoradas.
    - Uma tag pode apontar para mais de um tema (separado por virgula).
    - A mesma tag em varias linhas tem os temas JUNTADOS (nao sobrescreve).
    - A comparacao ignora maiusculas/minusculas, emojis e espacos duplicados.
    """
    dicionario = {}
    if not ARQUIVO_DICIONARIO.exists():
        print(f"[aviso] dicionario nao encontrado em {ARQUIVO_DICIONARIO}")
        return dicionario

    with open(ARQUIVO_DICIONARIO, encoding="utf-8-sig") as f:
        for linha_num, linha in enumerate(f, start=1):
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            if "=>" not in linha:
                print(f"[aviso] linha {linha_num} do dicionario ignorada "
                      f"(faltou '=>'): {linha}")
                continue
            tag, temas_str = linha.split("=>", 1)
            chave = normalizar_tag(tag)
            temas = [t.strip() for t in temas_str.split(",") if t.strip()]
            if chave and temas:
                destino = dicionario.setdefault(chave, [])
                for tema in temas:
                    if tema not in destino:
                        destino.append(tema)
    return dicionario


def carregar_lista_tags(arquivo, nome):
    """
    Le um arquivo de texto com uma tag por linha.
    Linhas vazias ou comecando com # sao ignoradas.
    Se o arquivo nao existir, segue normalmente com a lista vazia.
    """
    tags = set()
    if not arquivo.exists():
        print(f"[aviso] lista de tags {nome} nao encontrada em {arquivo}")
        return tags
    with open(arquivo, encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            tags.add(normalizar_tag(linha))
    return tags


def carregar_ignoradas():
    return carregar_lista_tags(ARQUIVO_IGNORADAS, "ignoradas")


def carregar_silenciadas():
    return carregar_lista_tags(ARQUIVO_SILENCIADAS_TXT, "silenciadas")


def tag_ignorada(tag, ignoradas):
    t = normalizar_tag(tag)
    return bool(
        t in ignoradas
        or t.startswith(PREFIXOS_IGNORADOS)
        or PADRAO_CITACAO.match(t)
    )


def tag_silenciada(tag, silenciadas):
    t = normalizar_tag(tag)
    return bool(
        t in silenciadas
        or PADRAO_ANO.match(t)
        or t.startswith(PREFIXOS_SILENCIADOS)
    )


def palavras_marcantes(titulos):
    # Conta em quantos titulos cada palavra aparece (uma vez por titulo),
    # ignorando palavras curtas e numeros puros.
    contagem = Counter()
    for titulo in titulos:
        palavras = set(re.findall(r"\w+", titulo.lower()))
        contagem.update(
            p for p in palavras
            if len(p) >= TAM_MIN_PALAVRA_TITULO and not p.isdigit()
        )
    return [p for p, _ in contagem.most_common(QTD_PALAVRAS_TITULO)]


def registrar_tag(destino, tag, post):
    info = destino.setdefault(tag, {
        "tag": tag,
        "ocorrencias": 0,
        "exemplo_post": post["url"],
        "titulos": [],
    })
    info["ocorrencias"] += 1
    info["titulos"].append(post["titulo"])


def finalizar_tags(destino):
    lista = list(destino.values())
    for info in lista:
        info["palavras_titulo"] = palavras_marcantes(info.pop("titulos"))
    return lista


def montar_artigos(posts, dicionario, ignoradas, silenciadas):
    artigos_por_tema = {}
    tags_sem_mapeamento = {}
    tags_silenciadas = {}
    posts_sem_tema = []
    ignorados_por_regra = 0

    for post in posts:
        temas_do_post = set()
        post_ignorado = any(tag_ignorada(t, ignoradas) for t in post["tags"])
        for tag in post["tags"]:
            temas = dicionario.get(normalizar_tag(tag))
            if temas:
                temas_do_post.update(temas)
            elif not post_ignorado:
                if tag_silenciada(tag, silenciadas):
                    registrar_tag(tags_silenciadas, tag, post)
                else:
                    registrar_tag(tags_sem_mapeamento, tag, post)

        if not temas_do_post:
            if post_ignorado:
                # post nao tematizavel (tem tag ignorada): fora do aviso
                ignorados_por_regra += 1
                continue
            # post sem nenhuma tag reconhecida: nao entra em nenhum tema
            # (registrado so para aviso; o site nao le este arquivo)
            posts_sem_tema.append({
                "titulo": post["titulo"],
                "url": post["url"],
                "data": post["data"],
                "tags": post["tags"],
            })
            continue

        item = {
            "titulo": post["titulo"],
            "url": post["url"],
            "data": post["data"],
        }
        for tema_id in temas_do_post:
            artigos_por_tema.setdefault(tema_id, []).append(item)

    pendentes = finalizar_tags(tags_sem_mapeamento)
    silenciadas_lista = finalizar_tags(tags_silenciadas)

    return (artigos_por_tema, pendentes, posts_sem_tema,
            ignorados_por_regra, silenciadas_lista)


def main():
    print("Buscando posts do blog...")
    try:
        posts = buscar_todos_os_posts()
    except urllib.error.URLError as e:
        print(f"[erro] Nao foi possivel acessar o feed do blog: {e}")
        sys.exit(1)

    print(f"{len(posts)} posts encontrados no feed.")

    # Mais recentes primeiro. A data "published" do Blogger vem em
    # formato ISO 8601, por isso a ordenacao por texto ja da a ordem
    # cronologica correta. Trocavel a qualquer momento (ver decisao
    # registrada no final desta resposta).
    posts.sort(key=lambda p: p["data"], reverse=True)

    dicionario = carregar_dicionario()
    print(f"{len(dicionario)} tags mapeadas no dicionario.")

    ignoradas = carregar_ignoradas()
    print(f"{len(ignoradas)} tags na lista manual de ignoradas.")

    silenciadas = carregar_silenciadas()
    print(f"{len(silenciadas)} tags na lista manual de silenciadas.")

    (artigos_por_tema, pendentes, sem_tema,
     n_ignorados, silenciadas_lista) = montar_artigos(
        posts, dicionario, ignoradas, silenciadas)

    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as f:
        json.dump(artigos_por_tema, f, ensure_ascii=False, indent=2, sort_keys=True)

    with open(ARQUIVO_PENDENTES, "w", encoding="utf-8") as f:
        json.dump(
            sorted(pendentes, key=lambda x: -x["ocorrencias"]),
            f, ensure_ascii=False, indent=2,
        )

    with open(ARQUIVO_SILENCIADAS, "w", encoding="utf-8") as f:
        json.dump(
            sorted(silenciadas_lista, key=lambda x: -x["ocorrencias"]),
            f, ensure_ascii=False, indent=2,
        )

    with open(ARQUIVO_SEM_TEMA, "w", encoding="utf-8") as f:
        json.dump(sem_tema, f, ensure_ascii=False, indent=2)

    total_vinculos = sum(len(v) for v in artigos_por_tema.values())
    print(f"Gerado {ARQUIVO_SAIDA}: {total_vinculos} vinculos post-tema "
          f"em {len(artigos_por_tema)} temas.")
    print(f"Gerado {ARQUIVO_PENDENTES}: {len(pendentes)} tags sem "
          f"mapeamento (revisar e adicionar ao dicionario).")
    print(f"Gerado {ARQUIVO_SILENCIADAS}: {len(silenciadas_lista)} tags "
          f"silenciadas (conferir de vez em quando).")
    print(f"Gerado {ARQUIVO_SEM_TEMA}: {len(sem_tema)} posts fora de "
          f"qualquer tema (revisar).")
    print(f"{n_ignorados} posts ignorados por regra (nao tematizaveis).")


if __name__ == "__main__":
    main()
