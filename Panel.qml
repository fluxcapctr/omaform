import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Omaform in the bar: a form icon, and a dropdown under it.
//
// This file sits at the root of the Omaform repository, so the widget runs
// Omaform from its own folder once ./install.sh has made the environment
// there, and falls back to an omaform on PATH. Until then the dropdown
// offers the install, in a terminal, where you can watch it.
Panel {
  id: root
  moduleName: "omaform"
  ipcTarget: "omaform"

  property bool checked: false
  property bool installed: false
  property var doctor: ({})
  property var recent: []

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color dim: Qt.darker(fg, 1.5)
  readonly property color tone: bar ? bar.barForeground : Color.foreground
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, "")

  // The manifest's version, to offer an update when the plugin moves ahead of
  // what ./install.sh last installed.
  property string pluginVersion: ""
  readonly property bool outdated: installed && pluginVersion !== ""
                                   && String(doctor.version || "") !== pluginVersion

  // install.sh puts Omaform in ~/.local/bin and its environment outside this
  // folder. Paths travel as arguments, never inside the shell text, so a file
  // name cannot become a command.
  readonly property string pick: 't=$1; shift; for c in "$HOME/.local/bin/$t" "${XDG_DATA_HOME:-$HOME/.local/share}/omaform-venv/bin/$t"; do if [ -x "$c" ]; then exec "$c" "$@"; fi; done; exec "$t" "$@"'

  function cli(args) { return ["sh", "-c", root.pick, "sh", "omaform"].concat(args) }
  function ui(args) { return ["sh", "-c", root.pick, "sh", "omaform-ui"].concat(args) }

  FileView {
    path: root.pluginDir + "/manifest.json"
    onLoaded: { try { root.pluginVersion = String(JSON.parse(text()).version || "") } catch (e) { } }
  }

  function refresh() {
    doctorProcess.running = true
    recentProcess.running = true
  }

  function openForm(path) {
    Quickshell.execDetached(root.ui(path ? [path] : []))
    root.close()
  }

  function install() {
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
                             root.pluginDir + "/install.sh"])
    root.close()
  }

  function agentLine() {
    var checks = root.doctor.checks || []
    for (var i = 0; i < checks.length; i++) {
      if (checks[i].name === "Omarchy agent")
        return checks[i].ok ? "AGENT · " + checks[i].detail.split(",")[0].toUpperCase()
                            : "AGENT · NOT CONNECTED"
    }
    return ""
  }

  Process {
    id: doctorProcess
    command: root.cli(["doctor", "--json"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          root.doctor = JSON.parse(text)
          root.installed = true
        } catch (e) {
          root.installed = false
        }
        root.checked = true
      }
    }
    onExited: function(code) { if (code !== 0) { root.installed = false; root.checked = true } }
  }

  Process {
    id: recentProcess
    command: root.cli(["recent", "--json", "--limit", "8"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.recent = JSON.parse(text).slice(0, 5) } catch (e) { root.recent = [] }
      }
    }
  }

  Component.onCompleted: doctorProcess.running = true
  onOpenedChanged: { if (opened) root.refresh() }

  // ------------------------------------------------------------ bar button

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    foreground: root.tone
    fontSize: Style.font.bodySmall
    tooltipText: ""
    dimmed: root.checked && !root.installed
    useActiveColor: false
    onPressed: function(b) { root.toggle() }
  }

  Item {
    id: anchorProbe
    anchors.right: button.right
    anchors.top: button.top
    width: 1
    height: button.height
  }

  // ------------------------------------------------------------ dropdown

  KeyboardPanel {
    id: panel
    anchorItem: anchorProbe
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(body.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: body
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        Text {
          text: "OMAFORM"
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.displayLarge
          font.bold: true
        }
        Text {
          width: parent.width
          elide: Text.ElideRight
          text: !root.checked ? "CHECKING…"
                : !root.installed ? "NOT INSTALLED YET"
                : root.outdated ? "UPDATE READY · " + root.pluginVersion
                : root.doctor.ready ? "READY · " + root.agentLine()
                : "SETUP NEEDED · omaform doctor"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 1.2
        }

        // Not installed: one button, and what it will do.
        Text {
          visible: root.checked && !root.installed
          width: parent.width
          wrapMode: Text.WordWrap
          text: "Fill out forms once and for all. Installing makes a Python environment "
                + "in this plugin's folder and adds Omaform to the launcher. No root access."
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
        Button {
          visible: root.checked && !root.installed
          width: parent.width
          text: "INSTALL OMAFORM"
          fontSize: Style.font.body
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          active: true
          onClicked: root.install()
        }

        Button {
          visible: root.outdated
          width: parent.width
          text: "UPDATE OMAFORM TO " + root.pluginVersion
          fontSize: Style.font.body
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.install()
        }

        // Installed: open, the newest forms, setup.
        Button {
          visible: root.installed
          width: parent.width
          text: "OPEN A FORM"
          fontSize: Style.font.body
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          active: true
          onClicked: root.openForm("")
        }
        Text {
          visible: root.installed && root.recent.length > 0
          text: "IN DOWNLOADS"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 1.2
        }
        Repeater {
          model: root.installed ? root.recent : []
          Button {
            required property var modelData
            width: body.width
            text: modelData.name + "  ·  " + modelData.blanks + " blanks"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.openForm(modelData.path)
          }
        }
        Button {
          visible: root.installed
          width: parent.width
          text: "SETUP · DETAILS, VAULT, OMARCHY AGENT"
          fontSize: Style.font.bodySmall
          foreground: root.fg
          fontFamily: root.fontFamily
          bordered: true
          onClicked: { Quickshell.execDetached(root.ui(["--setup"])); root.close() }
        }
      }
    }
  }
}
