#Requires -Version 5.0
<#
.SYNOPSIS
    Generates the ATP "Chapter 2: Windows Settings" DOCX from captured screenshots.
    Pure PowerShell / .NET — no external libraries required.

.DESCRIPTION
    Collects screenshots from all server folders and builds a DOCX file that matches
    the ATP documentation template structure.

.PARAMETER Screenshots
    Root folder containing server screenshot sub-folders (or a flat folder).
    E.g.  C:\ATP_Screenshots   or   \\fileserver\share\ATP

.PARAMETER Output
    Output .docx file path.

.PARAMETER Servers
    Comma-separated list of server names exactly as used when Capture-WindowsSettings.ps1 ran.
    Default: FM1,FM2,APP1,APP2,DB1,DB2

.EXAMPLE
    # Collect all servers into one combined folder then run:
    .\Generate-Report.ps1 -Screenshots C:\ATP_All -Output "ATP_Windows_Settings.docx"

    # Or if screenshots are in sub-folders per server:
    .\Generate-Report.ps1 -Screenshots C:\ATP_Screenshots -Output "ATP_Report.docx"
#>
param(
    [string]$Screenshots = "C:\ATP_Screenshots",
    [string]$Output      = "ATP_Windows_Settings_$(Get-Date -Format 'yyyyMMdd_HHmm').docx",
    [string]$Servers     = ""   # leave empty = auto-detect from sub-folders
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.Drawing

# ─── Document structure — headings exactly as in the Template ─────────────────
$SECTIONS = @(
    [ordered]@{ N=1;  H="Windows Version";                                               P="01_WindowsVersion"     }
    [ordered]@{ N=2;  H="Power Settings";                                                P="02_PowerSettings"      }
    [ordered]@{ N=3;  H="User Account Control settings";                                 P="03_UAC"                }
    [ordered]@{ N=4;  H="IE Enhanced Security settings";                                 P="04_IEEnhancedSecurity" }
    [ordered]@{ N=5;  H="Time and Date settings";                                        P="05_TimeDate"           }
    [ordered]@{ N=6;  H="Windows Activation";                                            P="06_Activation"         }
    [ordered]@{ N=7;  H="Device Manager";                                                P="07_DeviceManager"      }
    [ordered]@{ N=8;  H="Disk Management";                                               P="08_DiskManagement"     }
    [ordered]@{ N=9;  H="List of entire installed KB's on each Windows server";          P="09_KBList"             }
    [ordered]@{ N=10; H="List of entire installed programs on each Windows server";      P="10_InstalledPrograms"  }
    [ordered]@{ N=11; H="ipconfig /all command results of each servers";                 P="11_IpconfigAll"        }
    [ordered]@{ N=12; H="Disable firewall by local group policy.";                       P="12_Firewall"           }
    [ordered]@{ N=13; H="Disable windows defender anti-virus and real time protection."; P="13_Defender"           }
    [ordered]@{ N=14; H="14. Jumbo Frames / MTU Settings";                               P="14_MTU"                }
    [ordered]@{ N=15; H="15. APP TCP and DNS Parameters";                                P="15_TCPParams"          }
)

$APP_ONLY_SECTIONS = @(15)   # only APP servers for section 15

# ─── Helper: extract short display name from full server name ─────────────────
# NH-P11-FM1  →  FM1  |  NH-P11-APP2  →  APP2  |  FM1  →  FM1
function Get-ShortName([string]$fullName) {
    if ($fullName -match '-([^-]+)$') { return $matches[1] }
    return $fullName
}

# ─── Helper: sort key so order is FM → APP → DB (matches Template) ────────────
function Get-SortKey([string]$name) {
    $short = Get-ShortName $name
    $role  = if ($short -match '^FM')  { '1' }
             elseif ($short -match '^APP') { '2' }
             elseif ($short -match '^DB')  { '3' }
             else { '9' }
    $num   = if ($short -match '(\d+)$') { '{0:D3}' -f [int]$matches[1] } else { '000' }
    return "$role$num"   # e.g. FM1→"1001", APP2→"2002", DB1→"3001"
}

# ─── Auto-detect server names from sub-folder names ───────────────────────────
if ($Servers -eq "") {
    $detected = @(Get-ChildItem -Path $Screenshots -Directory -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty Name)
    if ($detected.Count -eq 0) {
        Write-Error "No sub-folders found under: $Screenshots`nEither the path is wrong or pass -Servers manually."
        exit 1
    }
    # Sort: FM first, then APP, then DB — matches Template order exactly
    $serverList = $detected | Sort-Object { Get-SortKey $_ }
    Write-Host "  Auto-detected servers (Template order): $($serverList -join ', ')" -ForegroundColor Yellow
} else {
    $serverList = $Servers -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
}

# 6 inches wide in EMUs  (1 inch = 914400 EMU)
$IMG_WIDTH_EMU = [long]5486400

# ─── Helper: find screenshot for a given section prefix + server name ─────────
function Find-Screenshot([string]$folder, [string]$prefix, [string]$server) {
    $patterns = @(
        (Join-Path $folder "${prefix}_${server}.png"),
        (Join-Path $folder "${prefix}_${server}_*.png"),
        (Join-Path (Join-Path $folder $server) "${prefix}_${server}.png"),
        (Join-Path (Join-Path $folder $server) "${prefix}_${server}_*.png"),
        "$folder\*\${prefix}_${server}.png",
        "$folder\*\${prefix}_${server}_*.png"
    )
    foreach ($pat in $patterns) {
        $hits = @(Get-ChildItem -Path $pat -ErrorAction SilentlyContinue)
        if ($hits.Count -gt 0) {
            return ($hits | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
        }
    }
    return $null
}

# ─── Helper: XML-escape a string ─────────────────────────────────────────────
function Escape-Xml([string]$s) {
    return $s.Replace('&','&amp;').Replace('<','&lt;').Replace('>','&gt;').Replace('"','&quot;').Replace("'",'&apos;')
}

# ─── XML fragments ────────────────────────────────────────────────────────────
function Get-ContentTypesXml {
@'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml"  ContentType="application/xml"/>
  <Default Extension="png"  ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"   ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
</Types>
'@
}

function Get-RootRelsXml {
@'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
'@
}

function Get-DocRelsXml([string[]]$imageRels) {
    $relLines = ($imageRels | Where-Object { $_ }) -join "`n  "
@"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"   Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
  $relLines
</Relationships>
"@
}

function Get-StylesXml {
@'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>
        <w:sz w:val="22"/>
        <w:szCs w:val="22"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:after="160" w:line="259" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>

  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:before="0" w:after="300"/>
      <w:jc w:val="left"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="56"/>
      <w:szCs w:val="56"/>
      <w:color w:val="1F3864"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:keepNext/>
      <w:spacing w:before="280" w:after="80"/>
      <w:outlineLvl w:val="0"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="36"/>
      <w:szCs w:val="36"/>
      <w:color w:val="2E74B5"/>
    </w:rPr>
  </w:style>

  <w:style w:type="paragraph" w:styleId="ServerLabel">
    <w:name w:val="ServerLabel"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:before="160" w:after="40"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="24"/>
      <w:szCs w:val="24"/>
    </w:rPr>
  </w:style>
</w:styles>
'@
}

function Get-SettingsXml {
@'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:defaultTabStop w:val="720"/>
  <w:compat>
    <w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>
  </w:compat>
</w:settings>
'@
}

# ─── Paragraph builders ───────────────────────────────────────────────────────
function Para-Title([string]$text) {
    $t = Escape-Xml $text
    "<w:p><w:pPr><w:pStyle w:val=`"Title`"/></w:pPr><w:r><w:t>$t</w:t></w:r></w:p>"
}

function Para-Heading1([string]$text) {
    $t = Escape-Xml $text
    "<w:p><w:pPr><w:pStyle w:val=`"Heading1`"/></w:pPr><w:r><w:t>$t</w:t></w:r></w:p>"
}

function Para-ServerLabel([string]$text) {
    $t = Escape-Xml "${text}:"
    "<w:p><w:pPr><w:pStyle w:val=`"ServerLabel`"/></w:pPr><w:r><w:t>$t</w:t></w:r></w:p>"
}

function Para-Image([string]$rId, [long]$cx, [long]$cy, [int]$id) {
@"
<w:p>
  <w:pPr><w:spacing w:before="0" w:after="120"/></w:pPr>
  <w:r>
    <w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0">
        <wp:extent cx="$cx" cy="$cy"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="$id" name="Picture $id"/>
        <wp:cNvGraphicFramePr>
          <a:graphicFrameLocks noChangeAspect="1"/>
        </wp:cNvGraphicFramePr>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic>
              <pic:nvPicPr>
                <pic:cNvPr id="$id" name="Picture $id"/>
                <pic:cNvPicPr><a:picLocks noChangeAspect="1" noChangeArrowheads="1"/></pic:cNvPicPr>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="$rId"/>
                <a:stretch><a:fillRect/></a:stretch>
              </pic:blipFill>
              <pic:spPr bwMode="auto">
                <a:xfrm><a:off x="0" y="0"/><a:ext cx="$cx" cy="$cy"/></a:xfrm>
                <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                <a:noFill/>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
"@
}

function Para-Missing([string]$label) {
    $t = Escape-Xml "  [ Screenshot missing for $label ]"
    "<w:p><w:r><w:rPr><w:color w:val=`"FF0000`"/><w:i/></w:rPr><w:t>$t</w:t></w:r></w:p>"
}

function Para-PageBreak {
    "<w:p><w:r><w:br w:type=`"page`"/></w:r></w:p>"
}

# ─── ZIP write helpers ────────────────────────────────────────────────────────
function Add-TextToZip([System.IO.Compression.ZipArchive]$zip, [string]$entryName, [string]$content) {
    $entry = $zip.CreateEntry($entryName)
    $enc   = New-Object System.Text.UTF8Encoding($false)  # UTF-8 without BOM
    $sw    = New-Object System.IO.StreamWriter($entry.Open(), $enc)
    $sw.Write($content)
    $sw.Dispose()
}

function Add-FileToZip([System.IO.Compression.ZipArchive]$zip, [string]$entryName, [string]$filePath) {
    $entry = $zip.CreateEntry($entryName)
    $dst   = $entry.Open()
    $bytes = [System.IO.File]::ReadAllBytes($filePath)
    $dst.Write($bytes, 0, $bytes.Length)
    $dst.Dispose()
}

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if (-not (Test-Path $Screenshots)) {
    Write-Error "Screenshots folder not found: $Screenshots"
    exit 1
}

Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Cyan
Write-Host "  ATP Report Generator" -ForegroundColor Cyan
Write-Host "  Screenshots : $Screenshots" -ForegroundColor Cyan
Write-Host "  Servers     : $Servers" -ForegroundColor Cyan
Write-Host "  Output      : $Output" -ForegroundColor Cyan
Write-Host ("=" * 60) -ForegroundColor Cyan
Write-Host ""

# ── Pass 1: resolve screenshot paths and compute image dimensions ──────────────
$imgIdx    = 0
$imageData = [System.Collections.Generic.List[hashtable]]::new()
$imgByKey  = @{}   # "$sectionN_$server" -> index in $imageData

foreach ($sec in $SECTIONS) {
    foreach ($srv in $serverList) {
        if ($APP_ONLY_SECTIONS -contains $sec.N -and $srv -notmatch "(?i)APP") { continue }

        $imgIdx++
        $imgPath = Find-Screenshot $Screenshots $sec.P $srv
        $rId     = "imgR$imgIdx"

        $entry = @{
            Idx       = $imgIdx
            RId       = $rId
            MediaName = "image${imgIdx}.png"
            Path      = $imgPath
            Label     = if ($sec.N -eq 15) { "$(Get-ShortName $srv) Parameters" } else { Get-ShortName $srv }
            CxEmu     = $IMG_WIDTH_EMU
            CyEmu     = [long]0
        }

        if ($imgPath) {
            $bmp = $null
            try {
                $bmp = [System.Drawing.Image]::FromFile($imgPath)
                $entry.CyEmu = [long]($IMG_WIDTH_EMU * [double]$bmp.Height / [double]$bmp.Width)
            } finally {
                if ($bmp) { $bmp.Dispose() }
            }
        } else {
            $entry.CyEmu = [long]($IMG_WIDTH_EMU * 9 / 16)   # fallback 16:9
        }

        $imageData.Add($entry)
        $imgByKey["$($sec.N)_$srv"] = $imageData.Count - 1
    }
}

# ── Pass 2: build document.xml ────────────────────────────────────────────────
$sb = [System.Text.StringBuilder]::new(512 * 1024)
[void]$sb.AppendLine('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
[void]$sb.AppendLine('<w:document')
[void]$sb.AppendLine('  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"')
[void]$sb.AppendLine('  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
[void]$sb.AppendLine('  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"')
[void]$sb.AppendLine('  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
[void]$sb.AppendLine('  xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">')
[void]$sb.AppendLine('<w:body>')

[void]$sb.AppendLine((Para-Title "Chapter 2: Windows Settings"))

for ($si = 0; $si -lt $SECTIONS.Count; $si++) {
    $sec = $SECTIONS[$si]

    [void]$sb.AppendLine((Para-Heading1 $sec.H))

    foreach ($srv in $serverList) {
        if ($APP_ONLY_SECTIONS -contains $sec.N -and $srv -notmatch "(?i)APP") { continue }

        $key   = "$($sec.N)_$srv"
        $entry = $imageData[$imgByKey[$key]]

        [void]$sb.AppendLine((Para-ServerLabel $entry.Label))

        if ($entry.Path) {
            [void]$sb.AppendLine((Para-Image $entry.RId $entry.CxEmu $entry.CyEmu $entry.Idx))
        } else {
            [void]$sb.AppendLine((Para-Missing $entry.Label))
        }
    }

    if ($si -lt ($SECTIONS.Count - 1)) {
        [void]$sb.AppendLine((Para-PageBreak))
    }
}

# Page/margin settings  (A4: 11906 x 16838 twips, margins ~1.9cm = 1080 twips)
[void]$sb.AppendLine(@'
<w:sectPr>
  <w:pgSz w:w="11906" w:h="16838"/>
  <w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="708" w:footer="708" w:gutter="0"/>
</w:sectPr>
'@)
[void]$sb.AppendLine('</w:body>')
[void]$sb.AppendLine('</w:document>')

$documentXml = $sb.ToString()

# ── Pass 3: build document relationships ──────────────────────────────────────
$imgRels = $imageData |
    Where-Object { $_.Path } |
    ForEach-Object {
        "  <Relationship Id=`"$($_.RId)`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image`" Target=`"media/$($_.MediaName)`"/>"
    }
$docRelsXml = Get-DocRelsXml $imgRels

# ── Pass 4: pack everything into a DOCX (ZIP) ─────────────────────────────────
if (Test-Path $Output) { Remove-Item $Output -Force }

# Ensure output directory exists
$outDir = Split-Path $Output -Parent
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
$fileStream = [System.IO.File]::Create($Output)
$zipMode    = [System.IO.Compression.ZipArchiveMode]::Create
$zip        = New-Object System.IO.Compression.ZipArchive($fileStream, $zipMode)

$found = 0; $missing = 0

try {
    Add-TextToZip $zip "[Content_Types].xml"           (Get-ContentTypesXml)
    Add-TextToZip $zip "_rels/.rels"                   (Get-RootRelsXml)
    Add-TextToZip $zip "word/document.xml"             $documentXml
    Add-TextToZip $zip "word/_rels/document.xml.rels"  $docRelsXml
    Add-TextToZip $zip "word/styles.xml"               (Get-StylesXml)
    Add-TextToZip $zip "word/settings.xml"             (Get-SettingsXml)

    foreach ($entry in $imageData) {
        if ($entry.Path) {
            Add-FileToZip $zip "word/media/$($entry.MediaName)" $entry.Path
            $found++
            Write-Host "  + $($entry.MediaName)  [$($entry.Label)]" -ForegroundColor DarkGray
        } else {
            $missing++
            Write-Host "  ! MISSING: $($entry.Label) (sec $($entry.Idx))" -ForegroundColor Red
        }
    }
}
finally {
    $zip.Dispose()
    $fileStream.Dispose()
}

Write-Host ""
Write-Host "  Images found   : $found" -ForegroundColor Green
if ($missing -gt 0) {
    Write-Host "  Images missing : $missing" -ForegroundColor Red
} else {
    Write-Host "  Images missing : 0" -ForegroundColor Green
}
$size = [math]::Round((Get-Item $Output).Length / 1MB, 1)
Write-Host ""
Write-Host "  Report saved: $Output  ($size MB)" -ForegroundColor Cyan
Write-Host ""
