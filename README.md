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

La app arranca en `http://0.0.0.0:5005`.

## Acceso desde otros dispositivos

Desde cualquier dispositivo conectado a la misma red local, abre el navegador y ve a:

```
http://<IP-DE-TU-PC>:5005
```

Para conocer tu IP local puedes usar `ifconfig` (macOS/Linux) o `ipconfig` (Windows).

## Donde se guardan los archivos

Los ePub subidos se almacenan en `storage/epubs/` dentro del proyecto.

## Soporte ACSM

La app puede convertir archivos `.acsm` (enlaces de descarga Adobe) directamente a ePub limpio sin DRM.

Flujo completo: ACSM -> fulfill Adobe -> descarga ePub con DRM -> eliminacion automatica de DRM -> ePub limpio listo para descargar en cualquier dispositivo.

Usa implementaciones Python puras:
- Protocolo ADEPT: basado en [acsm-calibre-plugin](https://github.com/Leseratte10/acsm-calibre-plugin)
- DRM removal: basado en [DeDRM_tools](https://github.com/apprenticeharper/DeDRM_tools)

No necesitas instalar libgourou ni compilar nada.

### Configuracion (una sola vez)

1. Abre la app en el navegador
2. Pulsa el enlace "Configuracion ACSM"
3. Autoriza un dispositivo Adobe (puedes usar autorizacion anonima o tu Adobe ID)
4. Una vez autorizado, puedes subir archivos `.acsm` y se convertiran a ePub limpio automaticamente

Las credenciales se guardan en `storage/adept/` (excluido de git).

## Notas

- Listado ordenado por fecha de subida (mas reciente primero).
- Archivos duplicados se renombran automaticamente con sufijo `_1`, `_2`, etc.
- Se aceptan archivos `.epub` y `.acsm`.
