## Adding Sound Effects

Let's say you're adding a "foo" button that says "FooBar", whose command description says "Play a Foo sound effect".

1. Create `sounds/foo/`
2. Create `sounds/foo/sound.json`:

```json
{
	"text": "FooBar",
	"name": "Foo"
}
```

3. Convert your audio file to MP3 format and save it as `sounds/foo/sound.mp3`. This will be used for uploading.
4. Convert your audio file to Opus format and save it as `sounds/foo/sound.opus`. This will be used for playing in voice.
