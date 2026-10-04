; Instalador de Celestia para Windows (NSIS). Lo llama instalador/construir.py:
;   makensis -DORIGEN=<carpeta Celestia> -DSALIDA=<exe> -DVERSION=<commit> -DICONO=<ico>
;
; Va a la carpeta del usuario (%LOCALAPPDATA%\Programs\Celestia) y no pide
; permisos de administrador: así cualquiera la instala, y Celestia puede escribir
; su memoria junto a ella. Reinstalar encima actualiza el código y deja intactas
; la memoria, las claves (.env) y lo que haya aprendido.

Unicode true
SetCompressor /SOLID lzma
!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "Celestia"
OutFile "${SALIDA}"
InstallDir "$LOCALAPPDATA\Programs\Celestia"
InstallDirRegKey HKCU "Software\Celestia" "Carpeta"
RequestExecutionLevel user
BrandingText "Celestia ${VERSION}"

!if "${ICONO}" != ""
  !define MUI_ICON "${ICONO}"
  !define MUI_UNICON "${ICONO}"
!endif
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION AbrirCelestia
!define MUI_FINISHPAGE_RUN_TEXT "Abrir Celestia ahora"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

Function AbrirCelestia
  Exec '"$INSTDIR\python\pythonw.exe" "$INSTDIR\lanzador.py"'
FunctionEnd

; Una Celestia abierta tiene sus archivos en uso (python.exe, pythonw.exe…) y
; Windows no deja sobrescribirlos: instalar encima daba «Error al abrir el
; archivo para escritura» y Enzo se quedó con que «no deja instalarlo» (3 oct
; 2026). Se cierra antes, sólo la que vive en ESTA carpeta: nunca otro Python.
; Por WMI (Win32_Process) y no con Get-Process: NSIS es de 32 bits y lanza el
; PowerShell de 32 bits, que ve los Python de 64 bits con la ruta VACÍA — el
; primer intento no cerraba nada (probado en el portátil y en GitHub).
!macro CerrarCelestia
  nsExec::Exec `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.Name -in 'python.exe','pythonw.exe' -and $$_.ExecutablePath -like '$INSTDIR\*' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
  Pop $0
  Sleep 1500
!macroend

Section "Celestia"
  DetailPrint "Cerrando Celestia si estaba abierta…"
  !insertmacro CerrarCelestia
  SetOutPath "$INSTDIR"
  File /r "${ORIGEN}\*"

  ; Acceso directo: Python sin ventana negra + el lanzador.
  CreateShortcut "$DESKTOP\Celestia.lnk" "$INSTDIR\python\pythonw.exe" '"$INSTDIR\lanzador.py"' \
                 "$INSTDIR\celestia.ico" 0
  CreateDirectory "$SMPROGRAMS\Celestia"
  CreateShortcut "$SMPROGRAMS\Celestia\Celestia.lnk" "$INSTDIR\python\pythonw.exe" \
                 '"$INSTDIR\lanzador.py"' "$INSTDIR\celestia.ico" 0
  CreateShortcut "$SMPROGRAMS\Celestia\Desinstalar Celestia.lnk" "$INSTDIR\Desinstalar.exe"

  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "Software\Celestia" "Carpeta" "$INSTDIR"
  !define CLAVE_DESINSTALAR "Software\Microsoft\Windows\CurrentVersion\Uninstall\Celestia"
  WriteRegStr HKCU "${CLAVE_DESINSTALAR}" "DisplayName" "Celestia"
  WriteRegStr HKCU "${CLAVE_DESINSTALAR}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${CLAVE_DESINSTALAR}" "Publisher" "Celestia"
  WriteRegStr HKCU "${CLAVE_DESINSTALAR}" "DisplayIcon" "$INSTDIR\celestia.ico"
  WriteRegStr HKCU "${CLAVE_DESINSTALAR}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegDWORD HKCU "${CLAVE_DESINSTALAR}" "NoModify" 1
  WriteRegDWORD HKCU "${CLAVE_DESINSTALAR}" "NoRepair" 1
  ; Remitente de avisos de Windows, con su nombre e icono (celestia_lib/push.py
  ; lo vuelve a poner por si acaso; aquí queda dado de alta desde el principio).
  WriteRegStr HKCU "Software\Classes\AppUserModelId\Celestia.App" "DisplayName" "Celestia"
  WriteRegStr HKCU "Software\Classes\AppUserModelId\Celestia.App" "IconUri" "$INSTDIR\imagenes\celestia_logo.png"

  ; /ABRIR: la actualización desde la propia app (celestia_lib/actualizar.py)
  ; instala en silencio; el instalador ha cerrado la Celestia abierta y aquí
  ; se vuelve a abrir, para que la persona no se encuentre la app cerrada.
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/ABRIR" $R1
  IfErrors +2
    Exec '"$INSTDIR\python\pythonw.exe" "$INSTDIR\lanzador.py"'
SectionEnd

Section "Uninstall"
  !insertmacro CerrarCelestia
  Delete "$DESKTOP\Celestia.lnk"
  RMDir /r "$SMPROGRAMS\Celestia"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Celestia"
  DeleteRegKey HKCU "Software\Celestia"
  DeleteRegKey HKCU "Software\Classes\AppUserModelId\Celestia.App"
  ; «Encenderme al iniciar el ordenador» (celestia_lib/escritorio.py).
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Celestia"

  ; Lo que es de la persona (conversaciones, claves, lo aprendido) sólo se
  ; borra si lo dice: desinstalar para reinstalar no debería costarle su memoria.
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "¿Borrar también tus conversaciones, tu memoria y tus claves?$\r$\n$\r$\nSi dices que no, se quedan en $INSTDIR y volverán si la instalas otra vez." \
    /SD IDNO IDYES borrar_todo
  ; Sólo el programa.
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\celestia_lib"
  Delete "$INSTDIR\*.py"
  Delete "$INSTDIR\Desinstalar.exe"
  Goto fin
  borrar_todo:
  RMDir /r "$INSTDIR"
  fin:
SectionEnd
