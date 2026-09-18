/** 路由守卫：未登录跳 /login 并记下来源；通过则渲染嵌套路由出口。 */
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../features/auth/authStore'

export default function RequireAuth() {
  const { isAuthenticated } = useAuth()
  const location = useLocation()

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return <Outlet />
}
