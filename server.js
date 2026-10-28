const express = require('express');
const { MongoClient } = require('mongodb');

const app = express();
const PORT = process.env.PORT || 3000;
const MONGO_URI = process.env.MONGO_URI;

let db_users = null;

if (MONGO_URI) {
    MongoClient.connect(MONGO_URI)
        .then(client => {
            const db = client.db();
            db_users = db.collection("persistent_users");
            console.log("[Mongo Express] Connected successfully.");
        })
        .catch(err => console.error("[Mongo Express Error]", err.message));
}

app.use(express.json());

app.get('/health', (req, res) => {
    return res.json({ status: "online" });
});

app.get('/api/user-points/:discord_id', async (req, res) => {
    const { discord_id } = req.params;
    if (!db_users) {
        return res.json({ points: 0, discord_id });
    }
    try {
        const doc = await db_users.findOne({ discord_id: String(discord_id) });
        return res.json({
            discord_id,
            points: doc ? (doc.points || 0) : 0,
            roblox_data: doc ? doc.roblox_data : null
        });
    } catch (err) {
        return res.status(500).json({ error: err.message, points: 0 });
    }
});

app.listen(PORT, () => {
    console.log(`[Render Express API] Running on port ${PORT}`);
});p
