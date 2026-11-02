[Setup]
; Preserve the previous installer identity so updates retain their installation record.
AppId=NileStore.Local
AppName=ALI BABA
AppVersion=3.0.1
DefaultDirName={localappdata}\Programs\ALI BABA
DefaultGroupName=ALI BABA
UsePreviousGroup=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AliBaba-Setup-Windows
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\AliBaba.exe
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
[Files]
Source: "..\dist\AliBaba\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\ALI BABA"; Filename: "{app}\AliBaba.exe"
Name: "{group}\ALI BABA — Подключение"; Filename: "{app}\AliBaba.exe"; Parameters: "--configure-network"
Name: "{autodesktop}\ALI BABA"; Filename: "{app}\AliBaba.exe"
[Run]
Filename: "{app}\AliBaba.exe"; Description: "Open ALI BABA"; Flags: nowait postinstall skipifsilent
