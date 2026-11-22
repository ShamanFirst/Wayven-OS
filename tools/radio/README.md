# Lista stacji radia internetowego

`build_catalog.py` buduje listę stacji dla modułu **Radio internetowe** w Wayven OS. Uruchamia go co tydzień zadanie [`radio-catalog.yml`](../../.github/workflows/radio-catalog.yml), a wynik trafia do przedwydania [`radio-catalog`](https://github.com/ShamanFirst/Wayven-OS/releases/tag/radio-catalog).

- **Źródła:** [Radio Browser](https://www.radio-browser.info/) (dane wolne do użytku, także w oprogramowaniu komercyjnym) i [Wikidata](https://www.wikidata.org/) (CC0).
- **Co trafia na listę:** stacje działające, w formatach MP3, Ogg (Vorbis, Opus) i FLAC, bez duplikatów. Kilka strumieni jednej stacji staje się wariantami jakości.
- **Czego nie ma:** stacji nadających tylko w AAC (wymaga licencji patentowej) i HLS. Logo stacji nie są kopiowane; urządzenie pobiera je na żywo z adresu podanego przez stację.
- **Podpis:** manifest jest podpisany kluczem ECDSA P-256 (sekret `RADIO_CATALOG_KEY`); urządzenie sprawdza go kluczem publicznym `radio-catalog-key.pub.pem`.

Źródłem tego skryptu jest repozytorium projektu (`tools/radio/`); tutaj leży kopia, z której korzysta zadanie.
