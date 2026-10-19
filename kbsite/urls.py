"""
URL configuration for kbsite project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import posixpath

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.staticfiles.views import serve as static_serve
from django.http import Http404
from django.urls import include, path, re_path

from kb.accesso import ModuloAccesso, ModuloAccessoAdmin

# anche /admin/login/ conta i tentativi sbagliati (vedi kb/accesso.py)
admin.site.login_form = ModuloAccessoAdmin


def static_admin(request, path):
    """I file statici dell'admin di Django, e nient'altro.

    Il controllo è sul percorso normalizzato: static_serve risolve i ".." dopo
    il match della rotta, e /static/admin/../index.html arriverebbe altrimenti
    ai file dell'app."""
    if not posixpath.normpath(path).startswith("admin/"):
        raise Http404
    return static_serve(request, path, insecure=True)


# Le rotte di accesso, elencate una per una invece di includere in blocco
# django.contrib.auth.urls. Quell'include esporrebbe anche il recupero
# password via email, che qui non può funzionare: non c'è nessun server di
# posta configurato per l'invio, e la pagina prometterebbe un messaggio che
# non arriva mai. Chi perde la password la fa reimpostare da un
# amministratore (`manage.py utente <nome> --password …`).
accounts = [
    path('accounts/login/',
         auth_views.LoginView.as_view(redirect_authenticated_user=True,
                                      authentication_form=ModuloAccesso),
         name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/password_change/',
         auth_views.PasswordChangeView.as_view(
             template_name='kb/password_change.html',
             success_url='/accounts/password_change/done/'),
         name='password_change'),
    path('accounts/password_change/done/',
         auth_views.PasswordChangeDoneView.as_view(
             template_name='kb/password_change_done.html'),
         name='password_change_done'),
]

urlpatterns = [
    path('admin/', admin.site.urls),
    *accounts,
    path('', include('kb.urls')),
    # I fogli di stile dell'amministrazione Django: con DEBUG spento non
    # verrebbero più serviti, e la pagina /admin/ resterebbe senza grafica.
    # insecure=True li serve comunque — ma SOLO quelli sotto admin/. Prima la
    # rotta prendeva tutto: /static/index.html e /static/app/index.js
    # rispondevano senza login, aggirando il controllo di /app/ e delle pagine.
    re_path(r'^static/(?P<path>admin/.*)$', static_admin),
]
