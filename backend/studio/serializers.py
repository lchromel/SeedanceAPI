from rest_framework import serializers

from .models import Asset


class MotionSelection(serializers.Serializer):
    asset = serializers.UUIDField()
    duration = serializers.IntegerField(min_value=4, max_value=120)


class ProjectConfig(serializers.Serializer):
    character = serializers.UUIDField(allow_null=True, default=None)
    clothing = serializers.ListField(child=serializers.UUIDField(), max_length=8, default=list)
    location = serializers.UUIDField(allow_null=True, default=None)
    duration = serializers.ChoiceField(choices=[30, 60, 90, 120], default=90)
    action = serializers.CharField(max_length=6000, allow_blank=True, default="")
    speech = serializers.CharField(max_length=6000, allow_blank=True, default="")
    motions = MotionSelection(many=True, default=list)

    def validate(self, data):
        user = self.context["user"]
        for kind in ("character", "clothing", "location"):
            ids = data[kind] if kind == "clothing" else ([data[kind]] if data[kind] else [])
            if Asset.objects.filter(owner=user, kind=kind, id__in=ids).count() != len(set(ids)):
                raise serializers.ValidationError("An asset is unavailable.")
        if (
            len(data["motions"]) > 10
            or sum(x["duration"] for x in data["motions"]) + data["duration"] > 300
        ):
            raise serializers.ValidationError("The full video must be at most 5 minutes.")
        for item in data["motions"]:
            preset = Asset.objects.filter(owner=user, kind="motion", id=item["asset"]).first()
            if not preset or item["duration"] > preset.duration:
                raise serializers.ValidationError("Motion duration exceeds its preset.")
        # JSONField stores only primitives.
        data["character"] = str(data["character"]) if data["character"] else None
        data["location"] = str(data["location"]) if data["location"] else None
        data["clothing"] = [str(x) for x in data["clothing"]]
        data["motions"] = [
            {"asset": str(x["asset"]), "duration": x["duration"]} for x in data["motions"]
        ]
        return data
