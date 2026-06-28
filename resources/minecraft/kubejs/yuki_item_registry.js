const YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH = '.yukibot/registries/items.json'
const YUKIBOT_ITEM_REGISTRY_SCHEMA_VERSION = 1
const BuiltInRegistries = Java.loadClass('net.minecraft.core.registries.BuiltInRegistries')

const itemIds = []
const blockItemIds = []

BuiltInRegistries.ITEM.keySet().forEach(id => {
    itemIds.push(String(id))
    if (BuiltInRegistries.BLOCK.containsKey(id)) {
        blockItemIds.push(String(id))
    }
})

itemIds.sort()
blockItemIds.sort()
JsonIO.write(YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH, {
    schema_version: YUKIBOT_ITEM_REGISTRY_SCHEMA_VERSION,
    generated_at_epoch_ms: Date.now(),
    item_ids: itemIds,
    block_item_ids: blockItemIds
})
console.info(`[YUKI_MC_ITEM_REGISTRY] wrote ${itemIds.length} item ids (${blockItemIds.length} blocks) to ${YUKIBOT_ITEM_REGISTRY_OUTPUT_PATH}`)
