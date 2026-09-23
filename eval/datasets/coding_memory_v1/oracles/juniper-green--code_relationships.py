"""Immutable oracle for juniper-green:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_scheduler_helper_green', service.relationship_target()


if __name__ == "__main__":
    main()
