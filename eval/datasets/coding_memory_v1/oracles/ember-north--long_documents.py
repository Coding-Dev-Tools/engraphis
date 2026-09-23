"""Immutable oracle for ember-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The archive path retains 30 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
