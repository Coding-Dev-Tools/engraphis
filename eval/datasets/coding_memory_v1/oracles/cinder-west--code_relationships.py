"""Immutable oracle for cinder-west:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_release_helper_west', service.relationship_target()


if __name__ == "__main__":
    main()
