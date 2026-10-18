// server.js
const express = require('express');
const cors = require('cors');
const sqlite3 = require('sqlite3').verbose(); // Use verbose for more detailed logging

const app = express();
const PORT = process.env.PORT || 3000; // Render will set the PORT environment variable

// Configure CORS
// Replace 'https://autoapplyjobai.netlify.app' with your actual Netlify URL
// In production, always specify exact origins, never use '*'
const corsOptions = {
  origin: ['https://autoapplyjobai.netlify.app', 'http://localhost:8080'], // Add your frontend URL here
  methods: 'GET,HEAD,PUT,PATCH,POST,DELETE',
  credentials: true,
  optionsSuccessStatus: 204
};
app.use(cors(corsOptions));

// Middleware to parse JSON request bodies
app.use(express.json());

// Database setup (using SQLite for simplicity; for Render production, you'd use PostgreSQL)
const db = new sqlite3.Database('./users.db', (err) => {
  if (err) {
    console.error('Error connecting to SQLite database:', err.message);
  } else {
    console.log('Connected to SQLite database.');
    // Create table if it doesn't exist
    db.run(`CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      toolkit_name TEXT,
      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )`, (createErr) => {
      if (createErr) {
        console.error('Error creating users table:', createErr.message);
      } else {
        console.log('Users table checked/created.');
      }
    });
  }
});

// Home route
app.get('/', (req, res) => {
  res.send('AI Toolkit Node.js Backend is running!');
});

// Endpoint to capture user data
app.post('/api/capture-user-data', (req, res) => {
  const { name, email, toolkit } = req.body; // 'toolkit' is what you named it in the frontend fetch

  if (!name || !email) {
    return res.status(400).json({ error: 'Name and email are required.' });
  }

  const toolkit_name = toolkit || null; // Handle if toolkit is not provided

  db.run(`INSERT INTO users (name, email, toolkit_name) VALUES (?, ?, ?)`,
    [name, email, toolkit_name],
    function(err) { // Use function(err) for 'this.lastID'
      if (err) {
        if (err.message.includes('UNIQUE constraint failed: users.email')) {
          return res.status(409).json({ error: 'Email already registered.' });
        }
        console.error('Database insertion error:', err.message);
        return res.status(500).json({ error: 'Failed to store data.', details: err.message });
      }
      res.status(201).json({ message: 'User data captured successfully.', id: this.lastID });
    }
  );
});

// Start the server
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});