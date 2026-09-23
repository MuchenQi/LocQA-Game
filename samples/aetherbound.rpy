# Aetherbound — chapter 1
#
# Sample source script. The branch structure is the point: three paths
# converge on gate_open, so a line there is read after any of three different
# predecessors, and cut_shrine_scene is content that was written, translated,
# and then orphaned when its entry point was removed.

define n = Character(None)
define k = Character("Kestrel")
define a = Character("Sister Aldis")


label start:
    scene bg gate_rain
    n "Rain had been falling on the Aether Gate for three days."

    show kestrel neutral
    k "You're late. I don't wait twice."

    show aldis calm
    a "Forgive him. He has been standing in the rain since dawn."

    menu:
        "Apologize for the delay.":
            jump path_apology

        "Ask what the Gate is.":
            jump path_question

        "Say nothing.":
            jump path_silence


label path_apology:
    k "Hmph. At least you know how to say it."
    a "That is more than most manage."
    jump gate_open


label path_question:
    a "The Gate is older than the Compact that binds it."
    k "It's a door. Doors open. That's all you need."
    jump gate_open


label path_silence:
    k "Nothing to say? Good. Talking wastes daylight."
    a "Silence has its own courtesy."
    jump gate_open


label gate_open:
    n "The Gate answered with a sound like a held breath."
    a "It recognizes you. That should not be possible."
    k "Everything's impossible until it isn't. Move."

    menu:
        "Step through the Gate.":
            jump chapter_two

        "Wait, and watch the light.":
            jump watch_light


label watch_light:
    n "The light folded inward, patient as water."
    a "We cannot stand here forever."
    jump chapter_two


label chapter_two:
    scene bg gate_far
    n "Beyond the Gate, the rain had never started."
    k "Stay close. I mean it."
    k "Whatever's on the other side, it knows we're here."
    k "And I don't much like being known."
    a "Then we shall be introduced properly."
    return


# Cut when the shrine subplot was dropped. Nothing jumps here any more, but
# the lines were translated and paid for.
label cut_shrine_scene:
    a "The shrine keeps records no one has read in a century."
    k "Then they're not records. They're dust."
    return
