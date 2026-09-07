import { useState } from 'react';
import { useAuth } from '../lib/authContext';
import './LoginScreen.css';

const MIN_PASSWORD = 10;

/**
 * Entrar o crear la cuenta. Un solo formulario con dos modos.
 *
 * Son dos modos y no dos pantallas porque el error más común acá es entrar por
 * la puerta equivocada —querer registrarse desde el login, o al revés— y con un
 * toggle la salida está a un clic, sin perder lo que ya se escribió.
 */
export function LoginScreen() {
  const { login, register, expired } = useAuth();
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const isRegister = mode === 'register';

  const submit = async (event) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await (isRegister ? register : login)(email.trim(), password);
    } catch (cause) {
      /* El mensaje del backend se muestra tal cual: está escrito para que lo lea
         una persona («Ya hay una cuenta con ese email»), y reemplazarlo por uno
         genérico tira a la basura la única pista útil. */
      setError(cause.message);
      setBusy(false);
    }
    /* Sin `setBusy(false)` en el camino feliz: al autenticar, este componente se
       desmonta y tocar su estado después sería un update sobre algo que ya no
       está en pantalla. */
  };

  const switchMode = () => {
    setMode(isRegister ? 'login' : 'register');
    setError(null);
  };

  return (
    <main className="login">
      <form className="login-card" onSubmit={submit}>
        <header className="login-head">
          <h1 className="login-title">Compras</h1>
          <p className="login-sub">
            {isRegister
              ? 'Creá tu cuenta para guardar tus listas, tarjetas y domicilios.'
              : 'Entrá para ver tus listas y comparar precios.'}
          </p>
        </header>

        {expired && !error && (
          <p className="login-note" role="status">
            Tu sesión venció. Entrá de nuevo.
          </p>
        )}

        <div className="field">
          <label className="field-label" htmlFor="login-email">
            Email
          </label>
          <input
            id="login-email"
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            /* `username` y no `email`: es lo que los gestores de contraseñas
               esperan para asociar la credencial al sitio. */
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck="false"
            required
            disabled={busy}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="login-password">
            Contraseña
          </label>
          <input
            id="login-password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            /* Distinguir los dos le dice al gestor de contraseñas si tiene que
               ofrecer la guardada o proponer una nueva. */
            autoComplete={isRegister ? 'new-password' : 'current-password'}
            minLength={isRegister ? MIN_PASSWORD : undefined}
            required
            disabled={busy}
          />
          {isRegister && (
            <p className="field-hint">
              Al menos {MIN_PASSWORD} caracteres. Una frase larga es más segura y
              más fácil de recordar que algo corto con símbolos.
            </p>
          )}
        </div>

        {error && (
          /* `alert` para que el lector de pantalla lo anuncie: si no, quien no ve
             la pantalla aprieta "Entrar" y no se entera de que falló. */
          <p className="login-error" role="alert">
            {error}
          </p>
        )}

        <button className="btn btn--primary btn--block" type="submit" disabled={busy}>
          {busy ? 'Un segundo…' : isRegister ? 'Crear cuenta' : 'Entrar'}
        </button>

        <button
          className="btn btn--quiet btn--block"
          type="button"
          onClick={switchMode}
          disabled={busy}
        >
          {isRegister ? 'Ya tengo cuenta' : 'Crear una cuenta'}
        </button>
      </form>
    </main>
  );
}
