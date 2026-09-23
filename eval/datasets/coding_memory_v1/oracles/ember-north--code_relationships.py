"""Immutable oracle for ember-north:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_archive_helper_north', service.relationship_target()


if __name__ == "__main__":
    main()
