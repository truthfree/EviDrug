import './App.css'
import { useAuthentication } from './authentication/useAuthentication'
import { PublicLanding } from './landing/PublicLanding'
import { VersionBadge } from './shared/VersionBadge'
import { Workspace } from './workspace/Workspace'

/** 인증 상태에 따라 공개 랜딩과 보호된 분석 작업공간을 전환한다. */
function App() {
  const authentication = useAuthentication()

  const content =
    authentication.state.status === 'signed_in' ? (
      <Workspace
        expiresAt={authentication.state.session.expires_at}
        isSigningOut={authentication.isSigningOut}
        signOutError={authentication.signOutError}
        onSignOut={authentication.signOut}
      />
    ) : (
      <PublicLanding
        isCheckingSession={authentication.state.status === 'checking'}
        isServiceUnavailable={authentication.state.status === 'unavailable'}
        serviceMessage={
          authentication.state.status === 'unavailable' ? authentication.state.message : null
        }
        onRetryConnection={authentication.retrySessionCheck}
        onSignIn={authentication.signIn}
        isSigningIn={authentication.isSigningIn}
        signInError={authentication.signInError}
        onClearSignInError={authentication.clearSignInError}
      />
    )

  return (
    <>
      {content}
      <VersionBadge />
    </>
  )
}

export default App
