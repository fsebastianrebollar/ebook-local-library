# Biblioteca ePub local

Servidor web casero para subir y descargar archivos ePub en tu red local.

## Instalacion

```bash
cd ebook_local_libary
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Ejecutar

```bash
python app.py
```

La app arranca en `http://0.0.0.0:5000`.

## Acceso desde otros dispositivos

Desde cualquier dispositivo conectado a la misma red local, abre el navegador y ve a:

```
http://<IP-DE-TU-PC>:5000
```

Para conocer tu IP local puedes usar `ifconfig` (macOS/Linux) o `ipconfig` (Windows).

## Donde se guardan los archivos

Los ePub subidos se almacenan en `storage/epubs/` dentro del proyecto.

## Notas

- Listado ordenado por fecha de subida (mas reciente primero).
- Archivos duplicados se renombran automaticamente con sufijo `_1`, `_2`, etc.
- Solo se aceptan archivos `.epub`.
