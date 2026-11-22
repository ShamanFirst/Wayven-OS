# Lista kanałów telewizji internetowej

`build_catalog.py` buduje listę kanałów dla modułu **TV internetowa** w Wayven OS. Uruchamia go co tydzień zadanie [`tv-catalog.yml`](../../.github/workflows/tv-catalog.yml), a wynik trafia do przedwydania [`tv-catalog`](https://github.com/ShamanFirst/Wayven-OS/releases/tag/tv-catalog).

- **Źródła:** [iptv-org](https://github.com/iptv-org/iptv) (lista publicznie dostępnych strumieni, dane w domenie publicznej, licencja Unlicense) i [Wikidata](https://www.wikidata.org/) (CC0): opis kanału, właściciel, rok startu.
- **Co trafia na listę:** kanały z działającym adresem HLS lub DASH, z podziałem na kraje i kontynenty. Kilka strumieni jednego kanału (SD, HD) staje się wariantami jakości; urządzenie wybiera wariant najbliższy 720p.
- **Czego nie ma:** kanałów dla dorosłych (oznaczenie `is_nsfw` i cała lista blokad iptv-org), kanałów zamkniętych, strumieni bez przypisanego kanału (nie da się ich sprawdzić pod kątem treści dla dorosłych) oraz strumieni RTMP, RTSP i SRT.
- **Logo** nie są kopiowane; urządzenie pobiera je z adresu podanego w iptv-org.
- **Podpis:** manifest jest podpisany tym samym kluczem ECDSA P-256 co lista stacji radiowych (sekret `RADIO_CATALOG_KEY`); urządzenie sprawdza go kluczem publicznym `catalog-key.pub.pem`.

Źródłem tego skryptu jest repozytorium projektu (`tools/iptv/`); tutaj leży kopia, z której korzysta zadanie. Skrypt czyta nazwy krajów z `tools/radio/countries.json`.
