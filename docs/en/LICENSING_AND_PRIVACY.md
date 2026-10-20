# Licensing and privacy

## MIT licence

The Cuthere source is released under the [MIT licence](../../LICENSE), copyright AutoVideoEdit contributors. When you use, modify or distribute it, keep the copyright and licence notices required by the licence.

Premiere, agent products, third-party dependencies and models keep their own terms; see [third-party notices](../../THIRD_PARTY_NOTICES.md).

## Local data

Default speech recognition uses local faster-whisper. Installing dependencies and preparing a model for the first time requires network access; model weights are downloaded separately and are never shipped with the source package.

Recordings, transcripts, timeline positions and edit records are stored in the project folder on this machine. The note handoff produces local files.

Once you hand a batch to an agent, whether media or text is sent to a cloud service depends on the agent, model and tools you chose. Select and configure those products according to your project's data requirements.

When reporting a problem, remove personal paths, client information and keys from logs or screenshots. Usually the versions, reproduction steps and error text are enough; the original media and project are not needed.

## Adobe and how it is installed

Cuthere is an independently developed third-party extension and is used with an Adobe Premiere Pro installation you obtained and licensed yourself.

This version is an unsigned CEP extension. The Windows and macOS installers enable the development-extension loading setting for the current user, and support a pre-install check, backups and uninstall/restore. It has not been through Adobe's marketplace review.

This project is not affiliated with, sponsored by, or certified by Adobe. Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries.

## References

- [MIT licence](https://opensource.org/license/mit)
- [Adobe CEP distribution and signing](https://developer.adobe.com/developer-distribution/experience-cloud/docs/guides/zxp/resources)
- [Adobe trademark rules](https://www.adobe.com/legal/permissions/trademarks.html)
