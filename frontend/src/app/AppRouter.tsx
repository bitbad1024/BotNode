/** 路由表：/login 公开；其余在登录守卫 + 后台布局之内。 */
import { Routes, Route, Navigate } from 'react-router-dom'
import RequireAuth from './RequireAuth'
import AppLayout from '../features/layout/AppLayout'
import LoginPage from '../features/auth/LoginPage'
import DashboardPage from '../features/dashboard/DashboardPage'
import RobotsPage from '../features/robots/RobotsPage'
import SchedulePage from '../features/schedule/SchedulePage'
import LogsPage from '../features/logs/LogsPage'

export default function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/robots" element={<RobotsPage />} />
          <Route path="/schedule" element={<SchedulePage />} />
          <Route path="/logs" element={<LogsPage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
