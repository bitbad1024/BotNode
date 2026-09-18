import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ThemeProvider } from './common/theme'
import { AuthProvider } from './features/auth/authStore'
import { ToastProvider } from './common/Toast'
import AppRouter from './app/AppRouter'

import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <ToastProvider>
            <AppRouter />
          </ToastProvider>
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
)
