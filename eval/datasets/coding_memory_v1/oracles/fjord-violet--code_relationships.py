"""Immutable oracle for fjord-violet:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_settlement_helper_violet', service.relationship_target()


if __name__ == "__main__":
    main()
