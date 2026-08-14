#!/usr/bin/env python3
"""
Busca TODOS os posts do blog f1descatholica (via feed publico do Blogger),
cruza as tags de cada post com o dicionario de temas (dados/dicionario-tags.txt)
e gera:
  - dados/artigos.json          -> lista de artigos organizada por tema
  - dados/tags-pendentes.json   -> tags encontradas nos posts que ainda nao
                                    tem correspondencia no dicionario

Este script roda automaticamente todo dia via GitHub Actions
(.github/workflows/atualizar-artigos.yml), mas tambem pode ser
rodado manualmente:  python scripts/atualizar_artigos.py
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BLOG_URL = "https://f1descatholica.blogspot.com"
FEED_URL = BLOG_URL + "/feeds/posts/default"
MAX_POR_PAGINA = 150  # limite seguro de itens por pagina do feed do Blogger

PASTA_DADOS = Path(__file__).resolve().parent.parent / "dados"
ARQUIVO_DICIONARIO = PASTA_DADOS / "dicionario-tags.txt"
ARQUIVO_SAIDA = PASTA_DADOS / "artigos.json"
ARQUIVO_PENDENTES = PASTA_DADOS / "tags-pendentes.json"


def buscar_pagina(start_index):
    url = (
        f"{FEED_URL}?alt=json&max-results={MAX_POR_PAGINA}"
        f"&start-index={start_index}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def extrair_posts_da_pagina(pagina):
    posts = []
    entradas = pagina.get("feed", {}).get("entry", [])
    for entrada in entradas:
        titulo = entrada.get("title", {}).get("$t", "").strip()

        link = None
        for l in entrada.get("link", []):
            if l.get("rel") == "alternate":
                link = l.get("href")
                break
        if not link:
            # posts sem link "alternate" nao sao artigos publicados normais
            continue

        tags = [
            c.get("term", "").strip()
            for c in entrada.get("category", [])
            if c.get("term")
        ]

        data_publicacao = entrada.get("published", {}).get("$t", "")

        posts.append({
            "titulo": titulo,
            "url": link,
            "tags": tags,
            "data": data_publicacao,
        })
    return posts


def buscar_todos_os_posts():
    todos = []
    start_index = 1
    while True:
        pagina = buscar_pagina(start_index)
        posts = extrair_posts_da_pagina(pagina)
        if not posts:
            break
        todos.extend(posts)
        if len(posts) < MAX_POR_PAGINA:
            break
        start_index += MAX_POR_PAGINA
    return todos


def carregar_dicionario():
    """
    Formato do arquivo dados/dicionario-tags.txt, uma linha por tag:

        tag exata do blogger => id_tema1, id_tema2

    - Linhas vazias ou comecando com # sao ignoradas.
    - Uma tag pode apontar para mais de um tema (separado por virgula).
    - A comparacao ignora maiusculas/minusculas.
    """
    dicionario = {}
    if not ARQUIVO_DICIONARIO.exists():
        print(f"[aviso] dicionario nao encontrado em {ARQUIVO_DICIONARIO}")
        return dicionario

    with open(ARQUIVO_DICIONARIO, encoding="utf-8") as f:
        for linha_num, linha in enumerate(f, start=1):
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            if "=>" not in linha:
                print(f"[aviso] linha {linha_num} do dicionario ignorada "
                      f"(faltou '=>'): {linha}")
                continue
            tag, temas_str = linha.split("=>", 1)
            tag = tag.strip()
            temas = [t.strip() for t in temas_str.split(",") if t.strip()]
            if tag and temas:
                dicionario[tag.lower()] = temas
    return dicionario


def montar_artigos(posts, dicionario):
    artigos_por_tema = {}
    tags_sem_mapeamento = {}

    for post in posts:
        temas_do_post = set()
        for tag in post["tags"]:
            temas = dicionario.get(tag.lower())
            if temas:
                temas_do_post.update(temas)
            else:
                info = tags_sem_mapeamento.setdefault(tag, {
                    "tag": tag,
                    "ocorrencias": 0,
                    "exemplo_post": post["url"],
                })
                info["ocorrencias"] += 1

        if not temas_do_post:
            # post sem nenhuma tag reconhecida: nao entra em nenhum tema
            continue

        item = {
            "titulo": post["titulo"],
            "url": post["url"],
            "data": post["data"],
        }
        for tema_id in temas_do_post:
            artigos_por_tema.setdefault(tema_id, []).append(item)

    return artigos_por_tema, list(tags_sem_mapeamento.values())


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

    artigos_por_tema, pendentes = montar_artigos(posts, dicionario)

    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as f:
        json.dump(artigos_por_tema, f, ensure_ascii=False, indent=2, sort_keys=True)

    with open(ARQUIVO_PENDENTES, "w", encoding="utf-8") as f:
        json.dump(
            sorted(pendentes, key=lambda x: -x["ocorrencias"]),
            f, ensure_ascii=False, indent=2,
        )

    total_vinculos = sum(len(v) for v in artigos_por_tema.values())
    print(f"Gerado {ARQUIVO_SAIDA}: {total_vinculos} vinculos post-tema "
          f"em {len(artigos_por_tema)} temas.")
    print(f"Gerado {ARQUIVO_PENDENTES}: {len(pendentes)} tags sem "
          f"mapeamento (revisar e adicionar ao dicionario).")


if __name__ == "__main__":
    main()
