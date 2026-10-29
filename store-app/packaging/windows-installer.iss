[Setup]
AppId=NileStore.Local
AppName=Nile Store
AppVersion=2.0.0
DefaultDirName={localappdata}\Programs\Nile Store
DefaultGroupName=Nile Store
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=NileStore-Setup-Windows
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\NileStore.exe
CloseApplications=yes
[Files]
Source: "..\dist\NileStore\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Nile Store"; Filename: "{app}\NileStore.exe"
Name: "{autodesktop}\Nile Store"; Filename: "{app}\NileStore.exe"
[Run]
Filename: "{app}\NileStore.exe"; Description: "Open Nile Store"; Flags: nowait postinstall skipifsilent
