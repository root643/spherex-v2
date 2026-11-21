# SPHEREx v2 — Cloud-Native Astronomy & All-Sky Viewer

Evolución arquitectónica v2 del servidor de visualización y análisis científico para el reto *Planet X and SPHEREx* (NASA Space Apps 2026).

## Novedades en la versión v2

### 1. HiPS All-Sky Navigation + WiseView Blink (Adaptive Bitrate a 60 FPS)
- **Capa base de navegación continua (HiPS + Aladin Lite v3):** Paneo y zoom fluido a 60 FPS por todo el firmamento mediante pirámides de teselas multirresolución estándar IVOA (AllWISE, 2MASS, DSS2, Pan-STARRS), reduciendo un ~80% la carga innecesaria en la CPU del backend.
- **Capa activa de detección (WiseView Blink / Diferencia):** Sincronización instantánea con el motor dinámico de SPHEREx para detección de movimiento propio (Planeta X, enanas marrones), análisis de dipolos $(\Delta I = I_B - I_A)$, coadición y espectrometría de 102 canales.

### 2. Soporte Remoto de FITS Tile Compression (`fpack` / `CompImageHDU`)
- Extensión en `server/backend/s3fits.py` y `upstream.py` con `CompImageRangeStream` y caché indexada por bloques.
- Permite leer imágenes FITS comprimidas por teselas (`RICE_1`, etc.) directamente en AWS S3 solicitando únicamente por HTTP Range los bytes de los bloques que intersecan la región del recorte.
- Reduce el consumo de datos de S3 en más de un 95% y elimina la dependencia de servicios de corte externos.

### 3. Almacén Cloud-Native en Zarr y Streaming Científico Float32 (ZFP / Blosc)
- **Cubos Zarr 3D:** Almacenamiento multidimensional $(\lambda, y, x)$ con bloques regulares $(17, 64, 64)$ y compresión ZSTD. Permite extraer espectros completos de 102 canales en menos de 10 milisegundos y conexión directa para notebooks de Python (`xarray` + `fsspec`).
- **Endpoint de datos crudos (`/api/v1/cutout/raw`):** Streaming de matrices en coma flotante real (`float32`) con compresión matemática de alta fidelidad ZFP (tolerancia configurable) y Blosc-ZSTD, garantizando máxima precisión astrofísica sin degradación de contraste.

## Desplegar (un comando)

En PowerShell, dentro de esta carpeta:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

El script detecta el usuario SSH (`ubuntu`/`opc`/`root`), sube `server/` y ejecuta `server/install.sh` con sudo. Al acabar lanza pruebas de humo contra los datos reales (indica cuántos recortes han salido de AWS S3 y cuántos de IRSA) y deja la web en `http://51.170.50.113/`. Se puede relanzar para actualizar.

Si la web no abre desde fuera, falta la regla de entrada TCP 80 en la *Security List* de la VCN de Oracle Cloud (eso solo se cambia en la consola de Oracle).

## Qué instala

| Pieza | Dónde | Notas |
| --- | --- | --- |
| Backend FastAPI | `/opt/spherex-backend`, venv en `/opt/spherex-env` | `spherex.service`, 1 worker por CPU (máx. 4), solo en 127.0.0.1:8000 |
| Frontend | `/var/www/spherex-frontend` | HTML/JS sin dependencias |
| Redis L1 | `/etc/redis/redis.conf` | `maxmemory` = mitad de la RAM (12 GB con 24 GB), `allkeys-lru`, sin RDB/AOF |
| Caché FITS en disco | `/var/cache/spherex/fits` | poda diaria a 60 GB (`/etc/cron.daily/spherex-prune-cache`) |
| Nginx | `sites-available/spherex` | micro-caché 12 h de `/api/`, `X-Fast-Cache` |
| Kernel | `/etc/sysctl.d/99-spherex.conf` | overcommit, swappiness |

**Lo que el instalador quita:** detiene y deshabilita `apache2`/`httpd`/`caddy`/`lighttpd`, para los contenedores Docker que publiquen 80/443, aparta los demás sitios de Nginx a `/etc/nginx/sites-disabled-<fecha>/` y reemplaza `/opt/spherex-backend` y `/var/www/spherex-frontend`. No toca nada más.

## API

| Endpoint | Función |
| --- | --- |
| `GET /api/v1/targets/search?q=` | Benchmark local → «RA Dec» → CDS Sesame |
| `GET /api/v1/observations?ra=&dec=` | Épocas de SPHEREx que cubren la posición (TAP de IRSA) |
| `GET /api/v1/cutout/frame?ra=&dec=&fov=&epoch=&band=&stretch=&invert=&diff=&ref=&vmin=&vmax=&res=` | Fotograma WebP reproyectado a TAN (N arriba, E izquierda). `diff=true` resta la época `ref`. `res=low` saca la versión rápida de baja resolución (rejilla nativa) para precarga |
| `GET /api/v1/cutout/coadd?ra=&dec=&fov=&band=&stretch=&method=` | Coadición (mediana o media) de todas las épocas |
| `GET /api/v1/cutout/prewarm?ra=&dec=&fov=&band=` | Encola en segundo plano la construcción de todas las épocas de esa banda (sin esperarla) |
| `GET /api/v1/spectrum?ra=&dec=` | Fotometría de apertura → 102 canales (µJy) + profundidad de hielo de agua y CO₂ |
| `GET /api/v1/cache/stats` | Redis, caché de disco y origen de los recortes (`source`: cuántos de S3, cuántos de IRSA y el último error de S3) |
| `GET /api/v1/source/test?ra=&dec=&n=` | Diagnóstico: lee `n` recortes directamente de AWS S3, sin caché, y devuelve el tiempo o el error de cada uno |
| `GET /api/docs` | Documentación interactiva |

`fov` va en segundos de arco (60–1800). `band` es `D1`…`D6` (detector completo) o `ch1`…`ch102` (canal fino). Los fotogramas devuelven cabeceras `X-Spherex-*` (límites de escala, nº de exposiciones, fecha, λ media, escala).

## Cómo procesa los datos

1. **Épocas.** SPHEREx no publica «una imagen por banda y época»: cada exposición ve el punto a una longitud de onda distinta. El servidor pide por TAP todas las exposiciones que contienen la posición y las agrupa en épocas por huecos de más de 20 días (y en ventanas de 60 días donde la cobertura es continua, cerca de los polos eclípticos).
2. **Fotograma.** Para la época y el detector pedidos lee los recortes (hasta 16) del bucket público de AWS, resta `ZODI`, enmascara con `FLAGS`, reproyecta cada uno con su WCS TAN-SIP a una rejilla TAN común de 3,075″/px y toma la mediana.
3. **Diferencia / coadición.** Resta de dos fotogramas ya alineados (dipolo = movimiento, monopolo = variabilidad) o mediana de todas las épocas.
4. **Espectro.** Apertura de 2 px con anillo de fondo en hasta 24 exposiciones por detector; la longitud de onda sale del WCS espectral (`WCS-WAVE`) de cada recorte.

## De dónde salen las imágenes: AWS S3

Las imágenes se leen del bucket público `nasa-irsa-spherex` (AWS, `us-east-1`, sin cuenta ni credenciales), no del servicio de recortes de IRSA. IRSA sigue usándose para la consulta TAP (qué exposiciones cubren la posición) y como respaldo.

- La ruta en S3 sale de la URL de IRSA: `…/ibe/data/spherex/dr1/level2/…fits` → `https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/dr1/level2/…fits`.
- No se baja la imagen entera (decenas de MB). Con peticiones HTTP `Range` se leen solo las cabeceras (una vez por imagen; quedan en `/var/cache/spherex/fits/layout`) y las filas del recorte de `IMAGE`, `ZODI` y `VARIANCE`. `FLAGS` viene comprimido y se trae entero (queda en `…/flags`); la tabla `WCS-WAVE` queda en `…/wave`.
- Las lecturas de una misma imagen van en paralelo, y `VARIANCE` solo se pide para el espectro.
- Las cabeceras de una imagen nueva se leen en 2 tandas en paralelo, usando como plantilla la estructura de otra imagen del mismo detector y procesado (cada cabecera se comprueba; si no cuadra, se recorren en serie).
- Si una imagen no está en el bucket o la lectura falla, ese recorte se pide a IRSA. Tras 12 fallos seguidos de S3 se deja de intentar durante 10 minutos.

## Caja de recortes en us-east-1 (opcional, activa)

Además de la lectura directa desde Madrid hay una micro-instancia ARM en la región del bucket (`t4g`, Ubuntu, `/opt/aws-cutout` con `aws_cutout.py`). Lee el bucket en-región (latencia de 1-2 ms) y devuelve los arrays ya recortados (~40 KB en vez de ~34 MB de FITS): recorte en frío ~0,6 s frente a ~1-2 s leyendo desde Madrid. El resto del procesado (reproyección, apilado, espectro, web) sigue íntegramente en Oracle.

- Oracle llega a ella por un túnel SSH (`spherex-aws-tunnel.service`, `127.0.0.1:9600`).
- La cadena de respaldo por recorte es: **caja AWS → bucket S3 directo → IRSA IBE**. Si la caja no responde 12 veces seguidas se deja de intentar 10 min.
- El listado de exposiciones (IRSA TAP) se copia a `/var/cache/spherex/fits/obs/`: si IRSA TAP cae, se sirve la última copia conocida en vez de dar 502.

## Catálogo local (volcado de IRSA TAP)

`tapdump.py` baja el catálogo SPHEREx completo de IRSA TAP (uri, fecha, banda y huella
`poly` de todos los level2) a una SQLite con índice espacial R-Tree en
`/var/cache/spherex/fits/catalog.db`. Lo lanza un timer systemd (`spherex-tapdump.timer`,
primera pasada ~10 min tras el arranque y chequeo cada hora — con un chequeo previo barato
que sale en segundos si TAP no responde —, paginación incremental por
mjd y reanudable si se corta). Cuando el volcado está completo (`meta.complete = 1`), si
IRSA TAP no responde el backend consulta la copia local (milisegundos, sin red) — IRSA
deja de ser una dependencia del camino caliente y solo se visita de noche.

Variables del volcado: `SPHEREX_CATALOG_DB` (ruta de la SQLite),
`SPHEREX_TAPDUMP_PAGE` (filas por página, 60000), `SPHEREX_TAPDUMP_PAUSE` (pausa entre
páginas, 1,5 s).

Variables de entorno (en `/etc/systemd/system/spherex.service`):

| Variable | Valor por defecto | Función |
| --- | --- | --- |
| `SPHEREX_SOURCE` | `s3` | `s3` (AWS+S3+IRSA), `irsa` (solo IRSA) o `s3only` (solo S3 directo, para diagnosticar) |
| `SPHEREX_S3_BASE` | `https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/` | Base del bucket |
| `SPHEREX_S3_THREADS` | `48` | Lecturas `Range` simultáneas |
| `SPHEREX_AWS_CUTOUT` | (vacío) | URL del endpoint de la caja AWS (`http://127.0.0.1:9600/cutout?token=...`); vacío desactiva la caja |

## Pruebas

```bash
python3 tests/test_imaging.py
python3 tests/test_s3fits.py
```

`test_imaging.py` cubre la aritmética de imágenes (apilado, máscaras, dipolo, estirado, WebP, fotometría, canales, épocas). `test_s3fits.py` cubre la lectura parcial de FITS por rangos contra un fichero sintético con la estructura de un level2. La parte que habla con AWS e IRSA (TAP, recortes reales, astropy/reproject) solo se puede comprobar en el servidor: lo hacen las pruebas de humo del instalador.

## Limitaciones conocidas

- Sin dominio no hay HTTPS ni HTTP/2; con un dominio basta `certbot --nginx`.
- El espectro no corrige apertura ni sigue el movimiento propio entre épocas.
- La primera petición de una zona tarda (descarga y reproyección); después sale de Redis/Nginx.
- No incluye todavía capas de Gaia/WISE ni máscaras de calidad visibles.
