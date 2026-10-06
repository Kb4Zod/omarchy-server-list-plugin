import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Server List: every local process listening on a TCP port, what it is, and a
// quick way to open or stop it. All discovery and signalling lives in
// bin/servers.py (stdlib Python reading /proc); this file is presentation.
Panel {
  id: root
  moduleName: "kb4zod.server-list"
  ipcTarget: "kb4zod.server-list"
  manageIpc: false

  property var servers: []
  property var systemServers: []
  property bool loaded: false
  property string error: ""
  property string statusText: ""
  property int cursorIndex: 0
  property bool cursorActive: false
  property bool systemExpanded: setting("showSystem", false) === true
  // Key of the row whose stop button is armed. A second press within
  // armTimer's window actually stops it, so a stray click can't kill T3 Code.
  property string armedKey: ""
  property var stopping: ({})

  readonly property int refreshIntervalSec: Math.max(2, Number(setting("refreshIntervalSec", 10)) || 10)
  readonly property bool showCount: setting("showCount", true) !== false
  readonly property bool hideWhenEmpty: setting("hideWhenEmpty", false) === true
  readonly property var ignoreList: {
    var list = setting("ignore", [])
    return Array.isArray(list) ? list.map(function(s) { return String(s).toLowerCase() }) : []
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar && bar.urgent.a > 0 ? bar.urgent : Color.urgent
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.6)
  readonly property string fontFamily: bar && bar.fontFamily !== "" ? bar.fontFamily : Style.font.family

  readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("bin/servers.py").toString().replace(/^file:\/\//, ""))

  // The list view is keyed: `rowKeys` only changes when servers come or go,
  // so the 3 s refresh updates rows in place (via `entries`) instead of
  // rebuilding every delegate and losing scroll and hover state.
  property var entries: ({})
  property var rowKeys: []
  readonly property var toggleEntry: ({ kind: "toggle", key: "toggle:system" })
  readonly property int exposedCount: servers.filter(function(s) { return s.exposed }).length

  function filtered(list) {
    if (ignoreList.length === 0) return list
    return list.filter(function(s) {
      var name = String(s.name || "").toLowerCase()
      var label = String(s.label || "").toLowerCase()
      return ignoreList.indexOf(name) === -1 && ignoreList.indexOf(label) === -1
    })
  }

  function refresh() {
    if (!listProc.running) listProc.running = true
  }

  function applyList(text) {
    try {
      var data = JSON.parse(text)
      servers = filtered(data.servers || [])
      systemServers = data.system || []
      error = ""
      rebuildRows()
    } catch (e) {
      error = "Could not read server list"
    }
    loaded = true
    clampCursor()
  }

  function rebuildRows() {
    var map = {}, keys = []
    servers.forEach(function(s) { map[s.key] = s; keys.push(s.key) })
    if (systemServers.length > 0) {
      map[toggleEntry.key] = toggleEntry
      keys.push(toggleEntry.key)
      systemServers.forEach(function(s) {
        map[s.key] = s
        if (systemExpanded) keys.push(s.key)
      })
    }
    entries = map
    if (keys.join("|") !== rowKeys.join("|")) rowKeys = keys
    clampCursor()
  }

  function clampCursor() {
    cursorIndex = Math.max(0, Math.min(cursorIndex, rowKeys.length - 1))
  }

  function moveCursor(delta) {
    if (rowKeys.length === 0) return
    if (!cursorActive) { cursorActive = true; return }
    cursorIndex = Math.max(0, Math.min(rowKeys.length - 1, cursorIndex + delta))
    armedKey = ""
  }

  function currentRow() {
    return cursorActive && cursorIndex >= 0 && cursorIndex < rowKeys.length ? (entries[rowKeys[cursorIndex]] || null) : null
  }

  function portsText(item) {
    return (item.ports || []).map(function(p) { return ":" + p }).join(" ")
  }

  function metaText(item) {
    var parts = []
    if (item.cwd && item.cwd !== "~") parts.push(item.cwd)
    else if (item.detail && item.kind !== "process") parts.push(item.detail)
    if (item.uptime) parts.push("up " + item.uptime)
    if (item.pid) parts.push("pid " + item.pid)
    return parts.join(" · ")
  }

  function openItem(item) {
    if (!item || !item.url) return
    Quickshell.execDetached(["xdg-open", item.url])
    close()
  }

  function copyUrl(item) {
    if (!item || !item.url) return
    Quickshell.execDetached(["wl-copy", item.url])
    statusText = "Copied " + item.url
    statusTimer.restart()
  }

  function requestStop(item) {
    if (!item || !item.stoppable || stopping[item.key]) return
    if (armedKey !== item.key) {
      armedKey = item.key
      armTimer.restart()
      var extra = item.alsoStops && item.alsoStops.length > 0 ? " (and " + item.alsoStops.join(", ") + ")" : ""
      statusText = "Press again to stop " + item.label + " " + portsText(item) + extra
      statusTimer.restart()
      return
    }
    armedKey = ""
    var next = Object.assign({}, stopping)
    next[item.key] = Date.now()
    stopping = next
    var cmd = item.kind === "container"
      ? ["python3", scriptPath, "stop-container", String(item.id)]
      : ["python3", scriptPath, "stop", String(item.pid), String(item.startTicks || "")]
    stopComponent.createObject(root, { command: cmd, itemKey: item.key, itemLabel: item.label + " " + portsText(item) })
  }

  function finishStop(key, label, ok, message) {
    if (!stopping[key]) return
    var next = Object.assign({}, stopping)
    delete next[key]
    stopping = next
    // The list shifts under the cursor once the row disappears; drop the
    // cursor so a repeated x x can't land on the neighbour.
    cursorActive = false
    statusText = ok ? "Stopped " + label : ("Couldn't stop " + label + (message ? ": " + message : ""))
    statusTimer.restart()
    refresh()
  }

  function activateCursor() {
    var row = currentRow()
    if (!row) return
    if (row.kind === "toggle") systemExpanded = !systemExpanded
    else openItem(row)
  }

  IpcHandler {
    target: "kb4zod.server-list"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function count(): string { return String(root.servers.length) }
    function state(): string {
      return JSON.stringify({ opened: root.opened, cursorActive: root.cursorActive, cursorIndex: root.cursorIndex,
                              armedKey: root.armedKey, stopping: Object.keys(root.stopping), status: root.statusText,
                              rows: root.rowKeys })
    }
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      cursorIndex = 0
      armedKey = ""
      refresh()
    }
  }
  onSystemExpandedChanged: rebuildRows()

  visible: !(hideWhenEmpty && loaded && servers.length === 0)
  implicitWidth: visible ? button.implicitWidth : 0
  implicitHeight: visible ? button.implicitHeight : 0

  Component.onCompleted: refresh()

  Process {
    id: listProc
    command: ["python3", root.scriptPath, "list"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyList(text) }
    onExited: function(code) { if (code !== 0) { root.error = "servers.py exited " + code; root.loaded = true } }
  }

  Component {
    id: stopComponent
    Process {
      id: stopProc
      property string itemKey: ""
      property string itemLabel: ""
      property string output: ""
      property bool streamed: false
      property int exitCode: -1
      running: true

      function settle() {
        if (!streamed || exitCode < 0) return
        var ok = exitCode === 0, msg = ""
        try { var r = JSON.parse(output); ok = !!r.ok; msg = r.error || "" } catch (e) { if (!ok) msg = "exit " + exitCode }
        root.finishStop(itemKey, itemLabel, ok, msg)
        stopProc.destroy()
      }

      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: { stopProc.output = text; stopProc.streamed = true; stopProc.settle() }
      }
      onExited: function(code) { stopProc.exitCode = code; stopProc.settle() }
    }
  }

  Timer { interval: (root.opened ? 3 : root.refreshIntervalSec) * 1000; running: true; repeat: true; onTriggered: root.refresh() }
  // A stop that never reports back (python3 missing, process wedged) must not
  // leave its row spinning forever.
  Timer {
    interval: 2000
    running: Object.keys(root.stopping).length > 0
    repeat: true
    onTriggered: {
      var now = Date.now()
      for (var key in root.stopping)
        if (now - root.stopping[key] > 15000) root.finishStop(key, key, false, "no response")
    }
  }
  Timer { id: armTimer; interval: 4000; onTriggered: root.armedKey = "" }
  Timer { id: statusTimer; interval: 4000; onTriggered: root.statusText = "" }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.showCount && root.servers.length > 0 && !vertical
      ? "󰒍 " + root.servers.length
      : "󰒍"
    slotSize: Style.bar.iconSlot * (root.showCount && root.servers.length > 0 && !vertical ? 1.8 : 1)
    opacity: root.loaded && root.servers.length === 0 ? 0.5 : 1
    tooltipText: root.opened ? "" : (root.servers.length === 1 ? "1 server running" : root.servers.length + " servers running")
    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveCursor(dy) }
      onActivateRequested: root.activateCursor()
      onDeleteRequested: { var r = root.currentRow(); if (r && r.kind !== "toggle") root.requestStop(r) }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r") root.refresh()
        else if (t === "s") root.systemExpanded = !root.systemExpanded
        else if (t === "c") root.copyUrl(root.currentRow())
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "Server List"
          meta: {
            if (!root.loaded) return "SCANNING…"
            var n = root.servers.length
            var s = n === 0 ? "NOTHING RUNNING" : (n + (n === 1 ? " RUNNING" : " RUNNING"))
            if (root.exposedCount > 0) s += " · " + root.exposedCount + " ON NETWORK"
            return s
          }
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              text: "󰒍"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
          trailingControl: Component {
            PanelActionButton {
              iconText: "󰑐"
              tooltipText: "Refresh (r)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.refresh()
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Text {
          visible: root.error !== "" || (root.loaded && root.servers.length === 0)
          width: parent.width
          text: root.error !== "" ? root.error : "No servers running."
          color: root.error !== "" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.WordWrap
        }

        ListView {
          id: list
          width: parent.width
          height: Math.min(contentHeight, Style.space(460))
          spacing: Style.space(4)
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
          model: root.rowKeys
          currentIndex: root.cursorActive ? root.cursorIndex : -1
          onCurrentIndexChanged: if (currentIndex >= 0) Qt.callLater(function() { list.positionViewAtIndex(list.currentIndex, ListView.Contain) })

          delegate: Loader {
            required property string modelData
            required property int index
            readonly property var entry: root.entries[modelData] || null
            readonly property int rowIndex: index
            width: list.width
            sourceComponent: modelData === root.toggleEntry.key ? toggleRow : serverRow
          }
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.statusText !== "" ? root.statusText : "enter open · x stop · c copy url · s system · r refresh"
          color: root.statusText !== "" ? root.foreground : root.dim
          opacity: root.statusText !== "" ? 1 : 0.7
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignHCenter
          elide: Text.ElideRight
        }
      }
    }
  }

  Component {
    id: toggleRow
    CursorSurface {
      id: tRow
      readonly property int rowIndex: parent ? parent.rowIndex : 0
      hasCursor: root.cursorActive && root.cursorIndex === rowIndex
      foreground: root.foreground
      implicitHeight: tLabel.implicitHeight + Style.spacing.rowPaddingX

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onContainsMouseChanged: if (containsMouse) { root.cursorActive = true; root.cursorIndex = tRow.rowIndex }
        onClicked: root.systemExpanded = !root.systemExpanded
      }

      Text {
        id: tLabel
        anchors.left: parent.left
        anchors.leftMargin: Style.space(10)
        anchors.verticalCenter: parent.verticalCenter
        text: (root.systemExpanded ? "󰅀  " : "󰅂  ") + "SYSTEM LISTENERS (" + root.systemServers.length + ")"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        font.letterSpacing: 1.2
      }
    }
  }

  Component {
    id: serverRow
    CursorSurface {
      id: sRow
      // Bound through the delegate Loader, so refreshes update in place.
      readonly property var entry: parent ? parent.entry : null
      readonly property int rowIndex: parent ? parent.rowIndex : 0
      readonly property bool armed: entry && root.armedKey === entry.key
      readonly property bool busy: entry && !!root.stopping[entry.key]

      hasCursor: root.cursorActive && root.cursorIndex === rowIndex
      foreground: root.foreground
      implicitHeight: content.implicitHeight + Style.spacing.rowPaddingX

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onContainsMouseChanged: if (containsMouse) { root.cursorActive = true; root.cursorIndex = sRow.rowIndex }
        onClicked: root.openItem(sRow.entry)
      }

      RowLayout {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(10)
        anchors.rightMargin: Style.space(6)
        spacing: Style.space(10)

        Text {
          text: sRow.entry ? sRow.entry.icon : ""
          color: root.foreground
          opacity: sRow.entry && sRow.entry.kind === "system" ? 0.6 : 1
          font.family: root.fontFamily
          font.pixelSize: Style.font.icon
          Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
          id: content
          Layout.fillWidth: true
          spacing: Style.space(1)

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(8)
            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: sRow.entry ? sRow.entry.label : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              visible: sRow.entry && sRow.entry.exposed
              text: "󰖟 LAN"
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              textFormat: Text.PlainText
              text: sRow.entry ? root.portsText(sRow.entry) : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            visible: text !== ""
            text: sRow.entry ? root.metaText(sRow.entry) : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideMiddle
          }

          Text {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            visible: text !== "" && sRow.hasCursor
            text: sRow.entry ? (sRow.entry.command || "") : ""
            color: root.dim
            opacity: 0.8
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        PanelActionButton {
          iconText: "󰖟"
          tooltipText: "Open " + (sRow.entry ? sRow.entry.url : "")
          visible: sRow.entry && sRow.entry.kind !== "system"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openItem(sRow.entry)
        }

        PanelActionButton {
          visible: sRow.entry && sRow.entry.stoppable
          enabled: !sRow.busy
          iconText: sRow.busy ? "󰔟" : (sRow.armed ? "󰄬" : "󰓛")
          tooltipText: sRow.armed ? "Click again to stop" : "Stop (x)"
          foreground: sRow.armed ? root.urgent : root.foreground
          hoverColor: root.urgent
          bordered: sRow.armed
          fontFamily: root.fontFamily
          onClicked: root.requestStop(sRow.entry)
        }
      }
    }
  }
}
