"""Todo lo que tiene que ver con Alexa.

Dos flujos que van en direcciones opuestas y conviene no confundir:

* `skill.py` + `signature.py` — **el skill propio**. Amazon invoca nuestro
  servidor cuando el usuario le habla al Echo, y nosotros escribimos en la lista
  de compras que ya vive en esta base. El vínculo se arma con el account linking
  de `web/oauth_alexa.py`, donde esta app hace de proveedor OAuth.

* `lwa.py` — **Login with Amazon**, esta app como cliente de Amazon. Existía para
  llamar a la Alexa Shopping and To-Do Lists API, que Amazon apagó el 1 de julio
  de 2024 junto con las List Skills. Quedó sin uso: los scopes
  `alexa::household:lists:*` se siguen otorgando y los toggles siguen en la
  consola, pero `api.amazonalexa.com/v2/householdlists` responde 403 y no hay
  reemplazo. Se borra cuando el skill esté probado en producción.

`interaction_model.es-MX.json` es lo que se pega en la consola de desarrollador,
en Build → JSON Editor. Vive acá y no en una carpeta de documentación porque
tiene que cambiar en el mismo commit que `skill.handle()`: un intent nuevo en el
código sin su utterance en el modelo es un intent que Alexa nunca va a mandar, y
al revés es un `IntentRequest` que llega y no lo atiende nadie.
"""
