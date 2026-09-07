import React from 'react';
import ReactDOM from 'react-dom/client';
// Las fuentes van empaquetadas, no traídas de Google: esta app se abre parado
// en un súper, con la señal que haya, y un webfont remoto es texto invisible
// hasta que baje. Sora solo carga los pesos que realmente se usan (display),
// no la variable completa: es la que se ve en cada número grande.
import '@fontsource-variable/inter';
import '@fontsource-variable/sora/wght.css';
import '@fontsource-variable/jetbrains-mono/wght.css';
import './styles/tokens.css';
import './styles/base.css';
import './components/ui/ui.css';
import { App } from './App';
import { AuthGate } from './components/AuthGate';
import { AuthProvider } from './hooks/useAuth';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {/* `AuthGate` envuelve a `App` en vez de que `App` consulte la sesión por su
        cuenta: así los hooks de la app —lista, perfil, tarjetas— no llegan a
        montarse sin sesión, y no hay que enseñarle a cada uno a distinguir "el
        backend falló" de "no estás logueado". */}
    <AuthProvider>
      <AuthGate>
        <App />
      </AuthGate>
    </AuthProvider>
  </React.StrictMode>,
);
