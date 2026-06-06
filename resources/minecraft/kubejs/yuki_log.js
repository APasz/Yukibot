var PREFIX = '[YUKI_MC_EVENT] '

function emit(type, data) {
    var out = {}

    out.type = String(type)
    out.time = Date.now()

    for (var key in data) {
        if (data.hasOwnProperty(key)) {
            out[key] = data[key]
        }
    }

    console.info(PREFIX + JSON.stringify(out))
}

function playerName(player) {
    if (player.username) return String(player.username)
    if (player.name && player.name.string) return String(player.name.string)
    return String(player)
}

function playerUuid(player) {
    if (player.uuid) return String(player.uuid)
    return ''
}

PlayerEvents.loggedIn(function (event) {
    emit('player_join', {
        player: playerName(event.player),
        uuid: playerUuid(event.player)
    })
})

PlayerEvents.loggedOut(function (event) {
    emit('player_leave', {
        player: playerName(event.player),
        uuid: playerUuid(event.player)
    })
})

PlayerEvents.chat(function (event) {
    emit('chat', {
        player: playerName(event.player),
        uuid: playerUuid(event.player),
        message: String(event.message)
    })
})

PlayerEvents.advancement(function (event) {
    emit('advancement', {
        player: playerName(event.player),
        uuid: playerUuid(event.player),
        advancement: String(event.advancement)
    })
})

EntityEvents.death(function (event) {
    var entity = event.entity

    if (!entity) return
    if (String(entity.type) != 'minecraft:player') return

    emit('player_death', {
        player: playerName(entity),
        uuid: playerUuid(entity),
        source: String(event.source)
    })
})
